import glob
import os
import re
import shutil
import threading
import time
import uuid
from urllib.parse import urlparse

import yt_dlp
from flask import Flask, render_template, request, jsonify, send_from_directory, abort
from werkzeug.utils import safe_join

app = Flask(__name__)
DOWNLOAD_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'downloads')
os.makedirs(DOWNLOAD_DIR, exist_ok=True)

# ---------------------------------------------------------------------------
# FFmpeg location
# Your WinGet install folder is set here. If you ever update FFmpeg and the
# version folder name changes, the app also auto-detects it (see find_ffmpeg_dir).
# Set to None to rely only on PATH / auto-detection.
# ---------------------------------------------------------------------------
FFMPEG_DIR = (r"C:\Users\Acer\AppData\Local\Microsoft\WinGet\Packages"
              r"\Gyan.FFmpeg_Microsoft.Winget.Source_8wekyb3d8bbwe\ffmpeg-9.0.2-full_build\bin")

# In-memory job store: job_id -> dict
JOBS = {}
JOBS_LOCK = threading.Lock()
JOB_TTL_SECONDS = 60 * 60  # forget finished jobs after 1 hour

ANSI_RE = re.compile(r'\x1b\[[0-9;]*m')


# ---------- helpers ----------

def _has_ffmpeg_tools(folder):
    if not folder or not os.path.isdir(folder):
        return False
    names = os.listdir(folder)
    lower = {n.lower() for n in names}
    has_ffmpeg = 'ffmpeg.exe' in lower or 'ffmpeg' in lower
    has_ffprobe = 'ffprobe.exe' in lower or 'ffprobe' in lower
    return has_ffmpeg and has_ffprobe


def find_ffmpeg_dir():
    """Return the folder that contains ffmpeg + ffprobe, or None."""
    # 1) the folder configured above
    if _has_ffmpeg_tools(FFMPEG_DIR):
        return FFMPEG_DIR
    # 2) environment variable
    env_dir = os.environ.get('FFMPEG_DIR')
    if _has_ffmpeg_tools(env_dir):
        return env_dir
    # 3) PATH
    found = shutil.which('ffmpeg')
    if found and _has_ffmpeg_tools(os.path.dirname(found)):
        return os.path.dirname(found)
    # 4) any WinGet install (survives version-folder changes)
    local = os.environ.get('LOCALAPPDATA')
    if local:
        pattern = os.path.join(local, 'Microsoft', 'WinGet', 'Packages', 'Gyan.FFmpeg*', '*', 'bin')
        for folder in sorted(glob.glob(pattern), reverse=True):
            if _has_ffmpeg_tools(folder):
                return folder
    return None


def sanitize_filename(name):
    name = re.sub(r'[\\/*?:"<>|]', "", name)
    name = re.sub(r'[\x00-\x1f]', "", name)
    name = name.strip().rstrip('. ')
    if len(name) > 120:
        name = name[:120].rstrip('. ')
    return name or "unknown"


def is_valid_url(url):
    try:
        p = urlparse(url)
        return p.scheme in ('http', 'https') and bool(p.netloc)
    except Exception:
        return False


def friendly_error(exc):
    """Turn yt-dlp / system exceptions into readable messages."""
    msg = ANSI_RE.sub('', str(exc)).strip()
    clean = re.sub(r'^ERROR:\s*', '', msg)
    low = msg.lower()
    if 'ffmpeg not found' in low:
        return 'FFmpeg was not found. Check FFMPEG_DIR at the top of app.py.'
    if 'ffmpeg' in low or 'ffprobe' in low:
        return 'FFmpeg problem: ' + clean[:250]
    if 'unsupported url' in low:
        return 'This link is not supported. Paste a direct video or music page link.'
    if 'private video' in low:
        return 'This video is private.'
    if 'sign in' in low or 'confirm your age' in low or 'age-restricted' in low:
        return 'This video requires sign-in or is age-restricted, so it cannot be downloaded.'
    if 'video unavailable' in low or 'not available' in low:
        return 'This video is unavailable (removed, region-locked, or blocked).'
    if 'http error 429' in low or 'too many requests' in low:
        return 'Too many requests. Wait a few minutes and try again.'
    if 'unable to download' in low or 'urlopen error' in low or 'getaddrinfo' in low \
            or 'timed out' in low or 'connection' in low:
        return 'Network problem. Check your internet connection and try again.'
    if 'permission denied' in low:
        return 'Permission denied while writing to the downloads folder.'
    if 'no space left' in low:
        return 'Not enough disk space.'
    return clean[:300] or 'Unknown error.'


def update_job(job_id, **fields):
    with JOBS_LOCK:
        job = JOBS.get(job_id)
        if job:
            job.update(fields)
            job['updated'] = time.time()


def cleanup_old_jobs():
    now = time.time()
    with JOBS_LOCK:
        stale = [jid for jid, j in JOBS.items()
                 if j['status'] in ('done', 'error') and now - j['updated'] > JOB_TTL_SECONDS]
        for jid in stale:
            del JOBS[jid]


def has_running_job():
    with JOBS_LOCK:
        return any(j['status'] in ('queued', 'fetching', 'downloading', 'converting')
                   for j in JOBS.values())


# ---------- download worker ----------

def run_download(job_id, url):
    try:
        ffmpeg_dir = find_ffmpeg_dir()
        if ffmpeg_dir is None:
            raise RuntimeError('ffmpeg not found')

        # Step 1: metadata only
        update_job(job_id, status='fetching', message='Fetching video info...', percent=0)
        with yt_dlp.YoutubeDL({'quiet': True, 'no_warnings': True, 'noplaylist': True}) as ydl:
            info = ydl.extract_info(url, download=False)
        if info is None:
            raise RuntimeError('Could not read information from this link.')
        if info.get('_type') == 'playlist' and info.get('entries'):
            info = next(iter(info['entries']))

        safe_title = sanitize_filename(info.get('title') or 'unknown')
        update_job(
            job_id,
            title=safe_title,
            thumbnail=info.get('thumbnail') or '',
            duration=info.get('duration') or 0,
            uploader=info.get('uploader') or info.get('channel') or '',
        )
        outtmpl = os.path.join(DOWNLOAD_DIR, safe_title + '.%(ext)s')

        # Progress hooks: download maps to 0-90%, conversion to 90-100%
        def progress_hook(d):
            if d['status'] == 'downloading':
                total = d.get('total_bytes') or d.get('total_bytes_estimate')
                done = d.get('downloaded_bytes') or 0
                pct = (done / total * 90) if total else 0
                update_job(
                    job_id,
                    status='downloading',
                    message='Downloading audio...',
                    percent=round(min(pct, 90), 1),
                    speed=ANSI_RE.sub('', d.get('_speed_str', '') or '').strip(),
                    eta=ANSI_RE.sub('', d.get('_eta_str', '') or '').strip(),
                )
            elif d['status'] == 'finished':
                update_job(job_id, status='converting',
                           message='Converting to MP3...', percent=90, speed='', eta='')

        def postprocessor_hook(d):
            if d['status'] == 'started':
                update_job(job_id, status='converting',
                           message='Converting to MP3...', percent=95)

        ydl_opts = {
            'format': 'bestaudio/best',
            'outtmpl': outtmpl,
            'ffmpeg_location': ffmpeg_dir,
            'postprocessors': [{
                'key': 'FFmpegExtractAudio',
                'preferredcodec': 'mp3',
                'preferredquality': '192',
            }],
            'noplaylist': True,
            'quiet': True,
            'no_warnings': True,
            'noprogress': True,
            'overwrites': True,
            'retries': 3,
            'fragment_retries': 3,
            'socket_timeout': 30,
            'progress_hooks': [progress_hook],
            'postprocessor_hooks': [postprocessor_hook],
        }

        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            ydl.download([url])

        # Step 3: locate the MP3
        mp3_name = safe_title + '.mp3'
        if not os.path.exists(os.path.join(DOWNLOAD_DIR, mp3_name)):
            candidates = [f for f in os.listdir(DOWNLOAD_DIR)
                          if f.endswith('.mp3') and f.startswith(safe_title[:50])]
            if not candidates:
                raise RuntimeError('Download finished but no MP3 was produced.')
            mp3_name = max(candidates,
                           key=lambda f: os.path.getmtime(os.path.join(DOWNLOAD_DIR, f)))

        update_job(job_id, status='done', message='Download complete!',
                   percent=100, filename=mp3_name, speed='', eta='')

    except Exception as e:
        update_job(job_id, status='error', message=friendly_error(e), speed='', eta='')


# ---------- routes ----------

@app.route('/')
def index():
    return render_template('index.html')


@app.route('/status')
def status():
    return jsonify({'ffmpeg': find_ffmpeg_dir() is not None})


@app.route('/download', methods=['POST'])
def download():
    data = request.get_json(silent=True) or {}
    url = (data.get('url') or '').strip()

    if not url:
        return jsonify({'success': False, 'error': 'Please enter a link.'}), 400
    if not is_valid_url(url):
        return jsonify({'success': False,
                        'error': 'That does not look like a valid link (must start with http:// or https://).'}), 400
    if has_running_job():
        return jsonify({'success': False,
                        'error': 'Another download is still in progress. Please wait for it to finish.'}), 409

    cleanup_old_jobs()
    job_id = uuid.uuid4().hex
    with JOBS_LOCK:
        JOBS[job_id] = {
            'status': 'queued', 'message': 'Starting...', 'percent': 0,
            'speed': '', 'eta': '', 'title': '', 'filename': '',
            'thumbnail': '', 'duration': 0, 'uploader': '',
            'updated': time.time(),
        }

    threading.Thread(target=run_download, args=(job_id, url), daemon=True).start()
    return jsonify({'success': True, 'job_id': job_id})


@app.route('/progress/<job_id>')
def progress(job_id):
    with JOBS_LOCK:
        job = JOBS.get(job_id)
        if not job:
            return jsonify({'success': False, 'error': 'Unknown job.'}), 404
        payload = {k: v for k, v in job.items() if k != 'updated'}
    payload['success'] = True
    return jsonify(payload)


@app.route('/list')
def list_files():
    try:
        files = [f for f in os.listdir(DOWNLOAD_DIR) if f.lower().endswith('.mp3')]
        files.sort(key=lambda x: os.path.getmtime(os.path.join(DOWNLOAD_DIR, x)), reverse=True)
        return jsonify(files)
    except OSError as e:
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/play/<path:filename>')
def play(filename):
    if safe_join(DOWNLOAD_DIR, filename) is None or not filename.lower().endswith('.mp3'):
        abort(400)
    if not os.path.isfile(os.path.join(DOWNLOAD_DIR, filename)):
        abort(404)
    return send_from_directory(DOWNLOAD_DIR, filename, mimetype='audio/mpeg')


@app.route('/save/<path:filename>')
def save(filename):
    if safe_join(DOWNLOAD_DIR, filename) is None or not filename.lower().endswith('.mp3'):
        abort(400)
    if not os.path.isfile(os.path.join(DOWNLOAD_DIR, filename)):
        abort(404)
    return send_from_directory(DOWNLOAD_DIR, filename, as_attachment=True)


@app.route('/delete/<path:filename>', methods=['DELETE'])
def delete(filename):
    path = safe_join(DOWNLOAD_DIR, filename)
    if path is None or not filename.lower().endswith('.mp3'):
        return jsonify({'success': False, 'error': 'Invalid filename.'}), 400
    if not os.path.isfile(path):
        return jsonify({'success': False, 'error': 'File not found.'}), 404
    try:
        os.remove(path)
        return jsonify({'success': True})
    except OSError as e:
        return jsonify({'success': False, 'error': f'Could not delete file: {e}'}), 500


@app.errorhandler(404)
def not_found(_):
    return jsonify({'success': False, 'error': 'Not found.'}), 404


@app.errorhandler(500)
def server_error(_):
    return jsonify({'success': False, 'error': 'Internal server error.'}), 500


if __name__ == '__main__':
    d = find_ffmpeg_dir()
    if d is None:
        print('WARNING: ffmpeg/ffprobe not found. Set FFMPEG_DIR at the top of app.py.')
    else:
        print('Using FFmpeg from:', d)
    app.run(debug=False, threaded=True, port=5000)