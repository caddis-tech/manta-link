cmake_minimum_required(VERSION 3.13)

# A bench tool for MANTA Link's silence reset. Not part of anything a boat runs.
include($ENV{PICO_SDK_PATH}/external/pico_sdk_import.cmake)

project(usb_stall_test C CXX ASM)
set(CMAKE_C_STANDARD 11)
pico_sdk_init()

add_executable(usb_stall_test usb_stall_test.c)
target_link_libraries(usb_stall_test pico_stdlib)
pico_enable_stdio_usb(usb_stall_test 1)
pico_enable_stdio_uart(usb_stall_test 0)
pico_add_extra_outputs(usb_stall_test)
