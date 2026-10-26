// Free Music Player — searches Jamendo (royalty-free / CC-licensed tracks
// from independent artists), downloads them locally, and plays them.
// No ads, no subscription — Jamendo's catalog is free-to-stream by design.
//
// Build: see README.md
// Usage:
//   ./music_player search "lofi beats"
//   ./music_player play <track_id>
//   ./music_player library

#include <iostream>
#include <fstream>
#include <sstream>
#include <string>
#include <vector>
#include <filesystem>
#include <thread>
#include <chrono>

#include <curl/curl.h>
#include "json.hpp"

#define MINIAUDIO_IMPLEMENTATION
#include "miniaudio.h"

namespace fs = std::filesystem;
using json = nlohmann::json;

// ---------------------------------------------------------------------
// Config
// ---------------------------------------------------------------------

// Jamendo offers a free public "client_id" for developers testing the API.
// For real / production use, register your own free client_id at
// https://devportal.jamendo.com/ (takes 2 minutes, no cost).
static const std::string JAMENDO_CLIENT_ID = "56d30c95"; // public test id
static const std::string JAMENDO_API_BASE  = "https://api.jamendo.com/v3.0";
static const std::string LIBRARY_DIR       = "library";

// ---------------------------------------------------------------------
// HTTP helpers (libcurl)
// ---------------------------------------------------------------------

static size_t WriteStringCallback(void* contents, size_t size, size_t nmemb, void* userp) {
    static_cast<std::string*>(userp)->append(static_cast<char*>(contents), size * nmemb);
    return size * nmemb;
}

static size_t WriteFileCallback(void* ptr, size_t size, size_t nmemb, FILE* stream) {
    return fwrite(ptr, size, nmemb, stream);
}

std::string httpGet(const std::string& url) {
    CURL* curl = curl_easy_init();
    std::string response;
    if (curl) {
        curl_easy_setopt(curl, CURLOPT_URL, url.c_str());
        curl_easy_setopt(curl, CURLOPT_WRITEFUNCTION, WriteStringCallback);
        curl_easy_setopt(curl, CURLOPT_WRITEDATA, &response);
        curl_easy_setopt(curl, CURLOPT_FOLLOWLOCATION, 1L);
        curl_easy_setopt(curl, CURLOPT_USERAGENT, "free-music-player/1.0");
        CURLcode res = curl_easy_perform(curl);
        if (res != CURLE_OK) {
            std::cerr << "curl error: " << curl_easy_strerror(res) << "\n";
        }
        curl_easy_cleanup(curl);
    }
    return response;
}

bool downloadFile(const std::string& url, const std::string& outPath) {
    CURL* curl = curl_easy_init();
    if (!curl) return false;
    FILE* fp = fopen(outPath.c_str(), "wb");
    if (!fp) { curl_easy_cleanup(curl); return false; }

    curl_easy_setopt(curl, CURLOPT_URL, url.c_str());
    curl_easy_setopt(curl, CURLOPT_WRITEFUNCTION, WriteFileCallback);
    curl_easy_setopt(curl, CURLOPT_WRITEDATA, fp);
    curl_easy_setopt(curl, CURLOPT_FOLLOWLOCATION, 1L);
    curl_easy_setopt(curl, CURLOPT_USERAGENT, "free-music-player/1.0");
    CURLcode res = curl_easy_perform(curl);

    fclose(fp);
    curl_easy_cleanup(curl);
    return res == CURLE_OK;
}

// ---------------------------------------------------------------------
// Jamendo API
// ---------------------------------------------------------------------

struct Track {
    std::string id;
    std::string name;
    std::string artist;
    std::string audioUrl;   // direct MP3 stream/download URL
    std::string license;
};

std::vector<Track> searchTracks(const std::string& query, int limit = 10) {
    std::ostringstream urlStream;
    urlStream << JAMENDO_API_BASE << "/tracks/"
              << "?client_id=" << JAMENDO_CLIENT_ID
              << "&format=json"
              << "&limit=" << limit
              << "&search=" << query // note: for spaces, URL-encode in real use
              << "&include=musicinfo"
              << "&audioformat=mp31";

    std::string raw = httpGet(urlStream.str());
    std::vector<Track> results;

    try {
        json j = json::parse(raw);
        for (auto& t : j["results"]) {
            Track track;
            track.id       = t.value("id", "");
            track.name     = t.value("name", "Unknown");
            track.artist   = t.value("artist_name", "Unknown Artist");
            track.audioUrl = t.value("audio", "");
            track.license  = t.value("license_ccurl", "");
            results.push_back(track);
        }
    } catch (const std::exception& e) {
        std::cerr << "Failed to parse Jamendo response: " << e.what() << "\n";
    }
    return results;
}

// ---------------------------------------------------------------------
// Playback (miniaudio)
// ---------------------------------------------------------------------

void playFile(const std::string& path) {
    ma_engine engine;
    if (ma_engine_init(nullptr, &engine) != MA_SUCCESS) {
        std::cerr << "Failed to init audio engine\n";
        return;
    }

    ma_sound sound;
    if (ma_sound_init_from_file(&engine, path.c_str(), 0, nullptr, nullptr, &sound) != MA_SUCCESS) {
        std::cerr << "Failed to load: " << path << "\n";
        ma_engine_uninit(&engine);
        return;
    }

    ma_sound_start(&sound);
    std::cout << "Playing... press Enter to stop.\n";

    // Poll until playback finishes or user hits Enter (non-blocking-ish)
    while (ma_sound_is_playing(&sound)) {
        std::this_thread::sleep_for(std::chrono::milliseconds(200));
    }

    ma_sound_uninit(&sound);
    ma_engine_uninit(&engine);
}

// ---------------------------------------------------------------------
// Commands
// ---------------------------------------------------------------------

void cmdSearch(const std::string& query) {
    std::cout << "Searching Jamendo for \"" << query << "\"...\n\n";
    auto tracks = searchTracks(query);

    if (tracks.empty()) {
        std::cout << "No results.\n";
        return;
    }

    for (auto& t : tracks) {
        std::cout << "[" << t.id << "] " << t.artist << " — " << t.name << "\n";
    }
    std::cout << "\nRun: ./music_player play <id> to download & play a track.\n";
}

void cmdPlay(const std::string& trackId) {
    // Re-query by id to get the audio URL (simple approach; a real app
    // would cache search results in a local DB/session).
    std::ostringstream urlStream;
    urlStream << JAMENDO_API_BASE << "/tracks/"
              << "?client_id=" << JAMENDO_CLIENT_ID
              << "&format=json"
              << "&id=" << trackId
              << "&audioformat=mp31";

    std::string raw = httpGet(urlStream.str());
    json j = json::parse(raw);

    if (j["results"].empty()) {
        std::cerr << "Track id not found.\n";
        return;
    }

    auto t = j["results"][0];
    std::string name     = t.value("name", "track");
    std::string artist   = t.value("artist_name", "artist");
    std::string audioUrl = t.value("audio", "");

    if (audioUrl.empty()) {
        std::cerr << "No playable audio URL for this track.\n";
        return;
    }

    fs::create_directories(LIBRARY_DIR);
    std::string outPath = LIBRARY_DIR + "/" + trackId + ".mp3";

    if (!fs::exists(outPath)) {
        std::cout << "Downloading \"" << name << "\" by " << artist << "...\n";
        if (!downloadFile(audioUrl, outPath)) {
            std::cerr << "Download failed.\n";
            return;
        }
    } else {
        std::cout << "Already downloaded — playing from local library.\n";
    }

    playFile(outPath);
}

void cmdLibrary() {
    if (!fs::exists(LIBRARY_DIR)) {
        std::cout << "Library is empty.\n";
        return;
    }
    std::cout << "Downloaded tracks:\n";
    for (auto& entry : fs::directory_iterator(LIBRARY_DIR)) {
        std::cout << "  " << entry.path().filename().string() << "\n";
    }
}

// ---------------------------------------------------------------------
// Entry point
// ---------------------------------------------------------------------

int main(int argc, char** argv) {
    curl_global_init(CURL_GLOBAL_DEFAULT);

    if (argc < 2) {
        std::cout << "Usage:\n"
                  << "  music_player search <query>\n"
                  << "  music_player play <track_id>\n"
                  << "  music_player library\n";
        return 1;
    }

    std::string cmd = argv[1];

    if (cmd == "search" && argc >= 3) {
        cmdSearch(argv[2]);
    } else if (cmd == "play" && argc >= 3) {
        cmdPlay(argv[2]);
    } else if (cmd == "library") {
        cmdLibrary();
    } else {
        std::cout << "Unknown command or missing argument.\n";
    }

    curl_global_cleanup();
    return 0;
}
