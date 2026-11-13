# Keep the GUI unlocked for remote writes

The Apple Notes write path uses UI scripting (Cmd+N, paste, Return, etc.). When
the macOS GUI session is locked, `loginwindow` owns the event queue and silently
drops those keystrokes — so a remote write looks like it succeeded but nothing
materializes.

To get SSH-driven writes to actually land, the Mac's GUI session must stay
unlocked while you're away. There is no Apple-supported way to write a
full-fidelity note while the screen is locked; the only workable answer is to
keep the screen unlocked.

This doc is about **how to keep the GUI unlocked safely**, not about the
security implications of doing so.

## The one critical toggle

System Settings → **Lock Screen** → *"Require password after screen saver
begins or display is turned off"* → **Never**.

Without this, even if the screensaver never starts and the display never
sleeps, macOS will still require a password after a short grace period — and
once it does, CGEvent keystrokes from the daemon silently fail.

## Supporting settings

Flip these alongside the one above so nothing you aren't watching kicks in:

- **Lock Screen → Start Screen Saver when inactive → Never**
- **Lock Screen → Turn display off when inactive → Never** (or a long timeout
  if you want the screen to dim; it just mustn't auto-lock)
- **Battery → Options → Prevent automatic sleeping on power adapter when the
  display is off → On** (ensures the system stays awake even if the display
  sleeps)

## Amphetamine (recommended)

[Amphetamine](https://apps.apple.com/us/app/amphetamine/id937984704) is the
most reliable way to *actively* keep the Mac awake while still letting the
display sleep when you want.

Settings that work well with remote writes:

- **Session duration:** Indefinite (or trigger-activated — see "Safer variants"
  below).
- **Prevent display sleep:** On.
- **Allow screen saver during session:** Off (redundant with the Lock Screen
  setting above, but safer).
- **Keep Mac awake when display is closed:** On (requires Amphetamine Pro).

Install **"Amphetamine Power Protect"** as well — it stabilises closed-display
sessions on Apple Silicon, where macOS is much more aggressive about forcing
sleep than on Intel.

## Apple Silicon lid-closed caveat

Apple Silicon Macs **force sleep when the lid closes without an external
display attached**. This is enforced at a level Amphetamine cannot override.

Two options:

1. **Keep the lid open.** Simplest and safest.
2. **Use a ~$5 HDMI "dummy plug" (fake-EDID dongle).** The Mac thinks a display
   is attached and Amphetamine can keep the session alive with the lid shut.
   Any generic "4K HDMI EDID emulator" on Amazon works.

Things that **do not** work on Apple Silicon:

- Clamshell mode without an external display.
- `sudo pmset -a disablesleep 1`.
- Legacy `AppleClamshellSleep` NVRAM tricks.
- "Wake for network access" — that only wakes the system, it does not unlock
  the GUI session.

## Safer variants (mix and match)

Always-unlocked is a real security concession. Narrow it to the cases you
actually need:

- **Lid-open-only.** Leave the lid open at your desk, closed when you travel.
  Simplest, no extra tooling.
- **Time-boxed via Amphetamine schedule trigger.** Only unlocked during the
  hours you expect to be sending remote writes (e.g. 9–22 on weekdays).
- **Home-WiFi-only via Amphetamine SSID / IP-range trigger.** Unlocked only
  while connected to your home network.
- **Tailscale-gated.** A tiny LaunchAgent that polls `tailscale status` every
  5 minutes and starts or ends an Amphetamine session via Amphetamine's
  AppleScript dictionary. Unlocked only while *you* (specifically) are logged
  into the tailnet.

These compose: Tailscale-gated + lid-open-only is usably narrow.

## Security tradeoff

Worth being honest about what "keep unlocked" costs you:

- Anyone with physical access to the Mac is logged in as you. That means
  Mail, Messages, Notes, open browser sessions, etc.
- The **Passwords app / Passkeys still prompt for Touch ID** on access, so
  saved credentials remain protected. Everything else is not.
- SSH and Screen Sharing continue to require their own auth, so remote
  attackers still need valid credentials — the unlocked session does not
  weaken those.

Pick the narrowest variant from the list above that still covers your use
case. For many users, lid-open-only at a home desk with a time-window
trigger is a reasonable balance.

## Why this matters for the MCP server

When the GUI session is locked, Apple Notes MCP write tools now raise
`SessionLockedError` instead of returning phantom success. The error message
points back to this document so the fix is discoverable.

If you see `SessionLockedError`, something in this doc is not configured the
way it needs to be — most commonly the "Require password" setting has been
flipped back on, or the Mac has slept and the grace period has elapsed.
