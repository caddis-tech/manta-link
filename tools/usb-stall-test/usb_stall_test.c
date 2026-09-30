/*
 * Reproduces the 2026-09-22 serial-link stall on demand, so MANTA Link's
 * silence reset can be tested on the bench. Never flash this to a boat.
 *
 * It prints a record every 5 s over USB serial, as the flight firmware does; a
 * record rather than a log line, because only records arm MANTA Link's reset.
 * Once a host has read WEDGE_AFTER_LINES of them, it claims its own CDC
 * transmit endpoint inside TinyUSB, so nothing leaves the transmit buffer
 * again. The device stays enumerated, the port stays open, and the host sees no
 * bytes and no error, which is what the boat did. A USB bus reset clears the
 * claim (TinyUSB's configuration_reset() zeroes every endpoint's state), the
 * next line goes out, and the wedge comes back WEDGE_AFTER_LINES lines into the
 * new session.
 */
#include <stdio.h>

#include "pico/stdlib.h"
#include "tusb.h"
/* TinyUSB internals, for usbd_edpt_claim(). Test use only. */
#include "device/usbd_pvt.h"

#define LINE_PERIOD_MS 5000
#define WEDGE_AFTER_LINES 12
/* USBD_CDC_EP_IN in pico-sdk's stdio_usb_descriptors.c. */
#define CDC_EP_IN 0x82

/* Lines the host has read this session. Only touched inside tud_task. */
static uint32_t delivered;

/* Every enumeration, including the one a USB reset forces, starts a session. */
void tud_mount_cb(void) { delivered = 0; }

/* Runs inside tud_task once a line has reached the host, after TinyUSB has
 * freed the endpoint, so the claim cannot fail. Lines printed while no host is
 * reading never get here, so it does not matter which side starts first. */
void tud_cdc_tx_complete_cb(uint8_t itf) {
    (void)itf;
    if (++delivered == WEDGE_AFTER_LINES) {
        (void)usbd_edpt_claim(0, CDC_EP_IN);
    }
}

int main(void) {
    stdio_init_all();

    for (uint32_t line = 1;; line++) {
        /* Numbered across sessions, so the host sees the gap a wedge leaves.
         * The explicit \r\n keeps stdio from splitting the line, and at under
         * 64 bytes it is one packet, so each line is one completion. */
        printf("{\"stall_test_line\":%lu,\"ms_since_boot\":%lu}\r\n",
               (unsigned long)line,
               (unsigned long)to_ms_since_boot(get_absolute_time()));
        sleep_ms(LINE_PERIOD_MS);
    }
}
