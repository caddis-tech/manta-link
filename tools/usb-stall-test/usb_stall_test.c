/*
 * Reproduces the 2026-09-22 serial-link stall on demand, so MANTA Link's
 * silence reset can be tested on the bench. Never flash this to a boat.
 *
 * It prints a line every 5 s over USB serial, as the flight firmware does.
 * After WEDGE_AFTER_LINES lines it claims its own CDC transmit endpoint inside
 * TinyUSB, so nothing leaves the transmit buffer again. The device stays
 * enumerated, the port stays open, and the host sees no bytes and no error,
 * which is what the boat did. A USB bus reset clears the claim (TinyUSB's
 * configuration_reset() zeroes every endpoint's state), the next line goes out,
 * and the wedge comes back WEDGE_AFTER_LINES lines into the new session.
 */
#include <stdio.h>

#include "pico/stdlib.h"
#include "tusb.h"
/* TinyUSB internals, for usbd_defer_func() and usbd_edpt_claim(). Test use only. */
#include "device/usbd_pvt.h"

#define LINE_PERIOD_MS 5000
#define WEDGE_AFTER_LINES 12
/* USBD_CDC_EP_IN in pico-sdk's stdio_usb_descriptors.c. */
#define CDC_EP_IN 0x82

/* Both written from inside tud_task, which runs from an interrupt here. */
static volatile uint32_t mounts;
static volatile bool wedged;

/* Every enumeration, including the one a USB reset forces, starts a session. */
void tud_mount_cb(void) { mounts++; }

/* Must run inside tud_task, which owns the endpoint state: never call it from
 * main. The claim fails while a transfer is in flight, so main asks again. */
static void wedge(void *unused) {
    (void)unused;
    if (usbd_edpt_claim(0, CDC_EP_IN)) {
        wedged = true;
    }
}

int main(void) {
    stdio_init_all();

    uint32_t line = 0;
    uint32_t session_line = 0;
    uint32_t session = mounts;

    while (true) {
        if (mounts != session) {
            session = mounts;
            session_line = 0;
            wedged = false;
        }

        /* Numbered across sessions, so the host sees the gap a wedge leaves. */
        printf("usb-stall-test line %lu\n", (unsigned long)++line);

        if (++session_line >= WEDGE_AFTER_LINES && !wedged) {
            usbd_defer_func(wedge, NULL, false);
        }
        sleep_ms(LINE_PERIOD_MS);
    }
}
