add_library(multilang MODULE multilang.cpp)
target_link_libraries(multilang PRIVATE Fcitx5::Core)
set_target_properties(multilang PROPERTIES PREFIX "")
install(TARGETS multilang DESTINATION "${FCITX_INSTALL_LIBDIR}/fcitx5")

configure_file(multilang-addon.conf.in multilang.conf @ONLY)
install(
    FILES "${CMAKE_CURRENT_BINARY_DIR}/multilang.conf"
    DESTINATION "${FCITX_INSTALL_PKGDATADIR}/addon"
)
