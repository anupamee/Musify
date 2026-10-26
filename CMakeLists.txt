cmake_minimum_required(VERSION 3.15)
project(free_music_player LANGUAGES C CXX)

set(CMAKE_CXX_STANDARD 17)
set(CMAKE_CXX_STANDARD_REQUIRED ON)

find_package(CURL REQUIRED)

# miniaudio needs pthreads + dl + m on Linux, and needs to link against
# audio backends (ALSA/PulseAudio are usually picked up automatically at
# runtime via dlopen, so no extra -l flags needed for those on most distros).
find_package(Threads REQUIRED)

add_executable(music_player src/main.cpp)

target_include_directories(music_player PRIVATE include)
target_link_libraries(music_player PRIVATE CURL::libcurl Threads::Threads)

if(UNIX AND NOT APPLE)
    target_link_libraries(music_player PRIVATE dl m)
endif()

if(APPLE)
    target_link_libraries(music_player PRIVATE
        "-framework CoreAudio"
        "-framework CoreFoundation"
        "-framework AudioToolbox"
    )
endif()
