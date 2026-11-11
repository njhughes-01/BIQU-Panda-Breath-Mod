# Requirements
## CC2 observation on reconnect
CC2 websocket initialization SHALL NOT send a printer-binding frame or get_settings request. It SHALL preserve the reported Dry configuration and running state. A receive timeout SHALL wait for further messages without sending unsupported polls.
## Safety and compatibility
Emergency lock SHALL still send heater-off after connecting. Legacy emulation SHALL retain its printer-binding handshake.
