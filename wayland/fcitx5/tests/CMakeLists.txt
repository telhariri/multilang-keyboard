add_executable(multilang_logic_tests test_logic.cpp)
target_include_directories(multilang_logic_tests PRIVATE "${CMAKE_CURRENT_SOURCE_DIR}/../src")
target_compile_features(multilang_logic_tests PRIVATE cxx_std_17)
add_test(NAME multilang_logic_tests COMMAND multilang_logic_tests)
