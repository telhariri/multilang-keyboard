cmake_minimum_required(VERSION 3.21)
project(fcitx5-multilang VERSION 1.1.0 LANGUAGES CXX)

include(CTest)

find_package(Fcitx5Core REQUIRED)
include("${FCITX_INSTALL_CMAKECONFIG_DIR}/Fcitx5Utils/Fcitx5CompilerSettings.cmake")

add_subdirectory(src)

if(BUILD_TESTING)
    add_subdirectory(tests)
endif()
