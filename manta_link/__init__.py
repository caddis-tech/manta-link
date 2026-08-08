"""MANTA Link: the single process that owns the Pico's USB serial port.

Only one process can usefully read a tty. The kernel hands each byte to exactly
one reader, and pyserial rewrites the shared termios on every open, so a second
opener both steals bytes and silently changes the survivor's read timing. This
package therefore owns the port outright and does every Pi-side job that needs
it, rather than letting a second program compete for the same device.

Today that is answering the Pico's boot-time request, and buffering the records
it sends for a worker to parse off the reader's thread. GPS enrichment, spooling
and upload land on top of the same reader.
"""

__version__ = "0.3.0"
