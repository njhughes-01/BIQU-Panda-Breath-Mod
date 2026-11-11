# Supervised drying
Keep explicitly started drying sessions alive across Panda stops/reconnects, with a fixed host deadline. Mod Stop cancels permanently.
Done when: focused simulated recovery, expiry, offline pending Stop, persistence and cancellation race checks pass, existing native/CC2 regressions pass, and independent review identifies no unresolved material bug. No deployment or live heater test in this change.
