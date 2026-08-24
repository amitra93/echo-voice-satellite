# Changelog

## 2.21.0-ea.5 (Early Access)

One change on ea.4, to what the Activity tab tells you.

### An interrupted answer now says how it was interrupted

Three different things stop a response, and until now they all recorded the
same way. They are now distinct:

| Outcome | What happened |
|---|---|
| **barged** | You said the wake word over it — a new turn followed |
| **cancelled** | You pressed the action button |
| **muted** | You muted the device, which also turns the microphone off |

ea.4 already recorded a wake word spoken over a *response* as **barged**. One
spoken while the Echo was still thinking recorded as **cancelled**, because
the two travel different paths internally and only one had been covered. Both
are barges and both now say so.

Recording all three the same way made a person talking over the assistant
indistinguishable from a button press. It also hid the fault this was built to
find: a device triggering on its own speech shows up as a barge followed by a
turn the barge started, and half of those were invisible.

**Expect your numbers to move.** Turns you interrupted on earlier builds were
counted as answered, and mutes during a response were counted as cancels.
Nothing about the fleet changed — only the counting. Older rows keep whatever
they were recorded as; nothing is rewritten, because a cause that was never
captured should not be invented later.

## 2.21.0-ea.4 (Early Access)

One fix on ea.3, for anyone who builds their own firmware.

### Deploying your own build no longer reports a rollback that did not happen

Uploading a locally compiled binary from **Updates → Local Build** worked, and
then said it had not:

```
⚠ Device reconnected on v2.12.0-63-g99628d3 — auto-rolled back
```

The device in that message is running exactly what was pushed. Nothing rolled
back.

Two causes. The controller reads the version out of the uploaded binary to
know what to expect on reboot, and it only recognised the older date-style
version string — so a build made from a clean checkout was labelled with a
placeholder the device could never report. And the dashboard treated any
mismatch as a rollback, when a rollback specifically means the device came
back on the firmware it had **before**.

Both fixed. A genuine rollback still says so. Anything else reports what the
device came back on and what was expected, without guessing, and explains the
placeholder when that was the cause.

Nothing here affects updating from a published release, which reads its
version from the release rather than from the binary.

## 2.21.0-ea.3 (Early Access)

One fix on ea.2, to a number you would otherwise have trusted.

### An answer you interrupt is recorded as interrupted

Cutting a response off mid-sentence — by saying the wake word over it — was
still recorded in the activity statistics as a completed answer.

2.21.0-ea.1 fixed this for an interruption during **thinking**, before the Echo
had started speaking. It missed the case it was actually written for: an
interruption during **playback**, once a response had begun. The two travel
different paths internally and only one was covered.

Measured here today: a response cut off mid-sentence, recorded as answered.

Both now record as **barged**. If you were reading the Activity tab across
earlier builds, turns you interrupted while the Echo was speaking were counted
as successful, and the totals will change once this is running — the fleet did
not change, the counting did.

## 2.21.0-ea.2 (Early Access)

Two fixes on top of 2.21.0-ea.1.

### Deploying a locally built firmware works again

Uploading your own compiled binary from the Updates tab failed with "an
internal error occurred". The controller was capping uploads at 1 MB against
a firmware roughly ten times that, so the file never reached the code meant
to handle it.

This broke on 2026-08-18 in 2.20.2, when a routine dependency update changed
how the web framework applies its size limit. Nothing else was affected —
updating from a published release goes by a different path and has worked
throughout. Only a locally built binary was blocked.

The limit is now 50 MB, and a file over it says so, with its size, instead of
reporting an internal error.

### Automatic update checks say when they are off

Setting `update_check_interval` to `0` stops the controller contacting GitHub
in the background. The Updates tab showed "No release info", which looks
exactly like GitHub being unreachable — so the tab you would open to find out
what was wrong could not tell you.

It now reads **Auto-checks off** beside the release line. **Check now** still
works and is unaffected: pressing it is a deliberate request, not background
traffic, and the result stays on screen until you press it again.

## 2.21.0-ea.1 (Early Access)

An Early Access build on top of 2.20.2. It supersedes 2.20.3-ea.1 and includes
everything in it — those notes are below.

Seven fixes, almost all of them things that failed quietly rather than
visibly. No database migration. One change needs new firmware and says so.

### A single bad message no longer drops the device

One malformed or unexpected control message could take a device's whole
connection down with it — the voice satellite, the Bluetooth proxy and the
audio channel together — and it reconnected every time it happened. The
message that triggered it in practice was a **playback statistics report**,
which is pure telemetry: the least important thing the device sends was able
to disconnect it.

Each message is now handled on its own. One that fails is logged with what it
was, and everything else carries on.

### Turning update checks off actually turns them off

Setting **Update check interval** to `0` was the obvious way to stop the
controller contacting GitHub, and it did the opposite: it removed the wait
between checks entirely, so the controller polled continuously until GitHub
rate-limited it. `0` now means off. A value that is not a number falls back to
the hourly default instead of silently killing update checking altogether.

### A dead music stream ends instead of hanging forever

If a Music Assistant stream stopped producing audio — an upstream failure
rather than the end of a track — playback waited indefinitely. Home Assistant
kept showing **playing** against silence, and nothing in the log said
otherwise.

A source that produces nothing for 30 seconds now ends the stream, tells Home
Assistant it is idle, and logs what happened. A stream that is merely slow
still recovers; the clock resets on every chunk that arrives.

### Media URLs are checked over HTTPS

Audio fetched over `https` did not verify the server's certificate. It does
now. If you stream from a server with a private or self-signed certificate,
point `EM_EXTRA_CA_CERT` at your CA and it will work as before. Playback
failures also now include the decoder's own error message, which previously
went nowhere.

### Interrupted answers are recorded as interrupted

A response cut off partway — by the button, or by the Echo hearing a wake word
mid-sentence — was recorded in the activity statistics as a completed answer.
A fleet interrupting a third of its responses read as a fleet answering
everything, which is how a real fault stayed hidden for two days. Those turns
now show as **barged**.

### The limiter's clipping counter reads correctly when it is off

Clipping caught while the limiter is switched off is the backstop doing its
job on a boosted EQ, not a fault. It was counted alongside genuine faults, so
a working setup looked broken. The two are now counted separately.

### The listening ring lights immediately — needs firmware v2.13.0

On an Echo doing its own wake word detection, the ring waited for a round trip
to the controller before lighting: measured at half a second, and longer on a
busy network. It now lights the moment the device hears you.

This one needs device firmware **v2.13.0** or later. On earlier firmware
everything behaves exactly as it does today, and the dashboard will offer the
update when it is published.

### Also

The command-line tools in `controller/tools/` now find the database under the
Home Assistant add-on as well as the standalone container.

## 2.20.3-ea.1 (Early Access)

An Early Access build on top of 2.20.2. The 2.20.2 notes below are the release
this builds on — you already have them if you were running an earlier build.

Two changes, both about the Echo hearing you. No database migration, no
firmware requirement, and nothing to do on your devices.

### Noise suppression no longer swallows quiet words

**Noise suppression** could cut speech to complete silence rather than just
turning it down. Measured on real turns: with it on, 8–15% of samples at a
healthy speaking level were digital silence, against 0.3% with it off. That is
the difference between a word sounding muffled and a word not being there at
all, and it lands hardest on exactly the quiet speech the setting is meant to
rescue.

Suppression is now limited to 20 dB. A passage the denoiser judges to be noise
is pushed well down instead of removed, and where it judges speech to be clean
it passes through completely untouched — so nothing that was working gets
quieter. Speech recognition stops improving well before 20 dB of noise
reduction, so the limit costs nothing that was being collected.

If you turned **Noise suppression** off because transcripts came back with
words missing, it is worth another try.

### Every Echo now knows all four wake words

Your Dot was given the recogniser for whichever wake word was selected when it
was provisioned. Choose a different one later and it needed that recogniser
copied across first — which normally happened, but left a gap: a device that
was **offline** when you changed the wake word was told to listen for a word it
did not have.

On a device doing its own wake word detection, that is a Dot that hears
nothing. Nothing warned, and the dashboard showed it as healthy.

All four stock wake words (Hey Jarvis, Alexa, Hey Mycroft, Hey Rhasspy) are now
installed on every device — 3 MB in total — so switching between them is
instant and cannot fail. Devices you provisioned earlier collect the missing
ones automatically the next time they connect, and any device found without the
recogniser it was told to use falls back to the controller listening on its
behalf, rather than going quietly deaf.

Custom wake words you have trained yourself are untouched and are still copied
across on selection.

## 2.20.2

**Better sound, and a long run of fixes.** Your Echo gets a speaker protection
stage that clears up the midrange and stops the equaliser distorting what it
boosts. Long answers no longer cut themselves off, wake words no longer fire at
nobody, interrupting a response no longer disconnects the device, Home
Assistant sidebar sign-in works again, and adding a second Echo no longer
overwrites the first.

**Two things to know before updating.** Your speaker will sound different —
the new protection stage is on by default. And the database migrates on first
start (a backup is taken automatically); for a small number of people that also
gives a device a new identity in Home Assistant, which is the last item here.
No firmware update is required and there is nothing to do on your devices.

### Your Echo sounds different, and it should sound better

Turning any equaliser band up used to push the audio past full scale, where it
was hard-clipped — measured at 4.7% of samples on a normal signal with a modest
bass boost, and 18% at the top of the sliders. A flat equaliser was unaffected,
so this only ever hit people who reached for the controls to improve their
sound.

There is now a limiter on the output, and a dynamic bass guard in front of it
that drops the low frequencies this driver physically cannot produce. The
second sounds backwards and is the bigger of the two: frequencies below about
115 Hz still move the cone even though you cannot hear them, and that movement
muddies everything above it — which is what "tin-can" actually is. Removing
them makes the midrange clearer and measurably *louder*, because the limiter no
longer has to hold the whole signal down to contain bass peaks nobody was going
to hear.

Both are on by default, as a single **Speaker protection** toggle under
Advanced → Playback. Leave it on. It was five separate controls during Early
Access and is now one, because none of the five could be judged by ear: the two
stages cancel out each other's most obvious effect, and the depth slider moves
the overall level by 0.14 dB across its entire range. Nothing was lost —
every setting still exists and still applies, and any value you had set is
still in force; the controls to change them again have gone from the dashboard.

Speaker settings also now take effect the moment you save them, mid-track,
rather than at the next track or the next restart.

### Long answers no longer cut themselves off

Ask for something lengthy — a story, a detailed explanation — and EchoMuse
would stop partway, sit with the ring lit, and end without finishing. It was
hearing its own voice as a wake word and cancelling itself. Measured on a real
device, a short reply peaked at 0.03 against the bar while a story reached
0.18 against a bar of 0.05.

Interrupting now needs two consecutive frames, and the default bar has moved
from 0.05 to 0.25. Talking over a response still works — real speech scores far
higher than the response does. **If you raised this setting yourself to stop
responses cutting out, you can put it back to the default.**

### Wake words no longer fire at nobody

The wake-word engine fills its buffer with random noise whenever it is reset,
and we scored that noise as if it were sound for the next 1.28 seconds. On one
device in one day that produced 19 wake events with nobody speaking, some
scoring higher than real speech. Raising your threshold would not have helped —
two of them cleared 0.80. Found, measured and fixed by @dmndru.

One consequence: interrupting a response is ignored for its first 1.28 seconds.

### Interrupting a response no longer disconnects the Echo

Saying the wake word mid-answer dropped the device off Home Assistant and
reconnected it a few seconds later — so the media player and voice assistant
flicked to unavailable each time. The cause was two lines of bookkeeping
sitting in an unreachable spot in the code, which only mattered when playback
was interrupted rather than finishing normally.

Also here: if the wake-word listener ever fails, it now restarts itself and
writes a loud error first, rather than leaving a device that hears you, lights
its ring, and never answers. A stuck "turn in progress" flag recovers the same
way. **We do not yet know what triggers that**, so if you see a device go deaf,
the log now contains the answer and we would like it.

### Home Assistant sign-in and admin rights

Opening EchoMuse from the Home Assistant sidebar failed and fell back to the
setup-token page — the lookup matching your HA account to an EchoMuse one
crashed on every request. Reported and diagnosed by @lennart24, fixed by
@finik.

Separately, if you had created a local EchoMuse account before ever opening the
panel through Home Assistant — including by copying `/data` across when moving
from the container, which the migration guide tells you to do — every Home
Assistant user was made read-only, permanently, with no way back but editing
the database by hand. Fixed, and existing installs in that state recover.

### Home Assistant behind your own certificate authority

If Home Assistant is served over HTTPS with a certificate from your own
internal CA, EchoMuse could not fetch the spoken response: the Echo woke up and
every turn ended silently. There is a new **Private CA certificate** option —
put the certificate (PEM) in Home Assistant's `ssl` folder and set the option
to `/ssl/<filename>`. On the standalone container, mount it and set
`EM_EXTRA_CA_CERT`. A missing or malformed file now stops the add-on starting
and says why.

Worth trying first, because it needs no certificate at all: if Home Assistant
itself still listens on plain HTTP behind something that terminates TLS, set
its *internal URL* to `http://<its-address>:8123`.

### Adding a second Echo no longer overwrites the first

Home Assistant identifies each satellite by a MAC address we derive from the
Echo's serial number. That derivation stripped letters out of the serial, so
two Echoes from the same batch differing only in their trailing characters
collapsed onto the same address and Home Assistant concluded they were one
device. Found and diagnosed by @lennart24.

That identity is now assigned once and stored, so a fix reaches only the
devices that need it. **On upgrade, every device keeps the identity it has
today unless it is in a colliding pair** — in which case the older device keeps
it and the other takes a new one, appearing in Home Assistant as a new device.
That device's entity IDs change and any automation naming them needs
repointing; it is the device that was being overwritten, so it had no working
entities to lose.

Also in this release: EchoMuse now carries a LICENSE and a contributing guide.

## 2.20.2-ea.7 (Early Access)

One addition, for people running Home Assistant over HTTPS with their own
certificate authority.

### Private certificate authorities are now supported

If Home Assistant is served over HTTPS using a certificate from your own
internal CA, EchoMuse could not fetch the spoken response. The controller
started normally and the Echo woke up, but every turn ended silently — the
audio fetch failed certificate verification, and nothing on screen said so.

There is a new **Private CA certificate** option. Put your CA certificate (PEM
format) in Home Assistant's `ssl` folder and set the option to
`/ssl/<filename>`. On the standalone container, mount the certificate and set
`EM_EXTRA_CA_CERT` to its path inside the container.

If the file is missing, unreadable, or not in PEM format, the add-on now
**fails to start and says why**, rather than starting and failing on every
voice turn afterwards with an error nothing connects back to the setting.

**Worth trying first, because it needs no certificate at all:** if Home
Assistant itself still listens on plain HTTP and something in front of it
handles TLS, setting Home Assistant's *internal URL* to
`http://<its-address>:8123` avoids the problem entirely — EchoMuse is on your
network, and the audio fetch is a local hop.

Nothing else changed, and nobody who is not using a private CA is affected. No
database migration, no firmware requirement, nothing to do on your devices.

## 2.20.2-ea.6 (Early Access)

One fix: interrupting a long answer dropped the device off Home Assistant.

### Barging in during a response disconnected the Echo

Say the wake word while EchoMuse is answering and the device would drop its
connection and reconnect a few seconds later. Everything came back on its own,
but Home Assistant lost the satellite briefly each time — so the media player
and the voice assistant flicked to unavailable, and anything mid-flight was
lost.

The cause was two lines of internal bookkeeping that had been sitting in an
unreachable spot in the code for months, so a value the controller expected
after playback was never set up. It only mattered when playback was
*interrupted* rather than finishing normally, which is why it surfaced now that
interrupting works properly.

Interrupting a response is now what it should be: the answer stops, your new
request is heard, and the device stays connected throughout.

Also in this release, a guard against the same class of mistake anywhere in the
controller, and a limit on how fast the wake-word listener may restart itself
if it ever fails repeatedly — that recovery was added in 2.20.2-ea.5 and could
have monopolised the controller in the worst case.

Nothing required of you: no database migration, no firmware requirement, and
nothing to do on your devices.

## 2.20.2-ea.5 (Early Access)

One fix, and it is for the fault that showed up while testing 2.20.2-ea.4.

### A device could stop responding to the wake word until the add-on restarted

It went quiet with nothing in the log to say so, and — the confusing part —
the Echo itself carried on detecting the wake word perfectly. On devices doing
their own wake word detection, the Echo hears you, decides it heard you, and
tells the controller; the part of the controller that acts on that had stopped
running. So the device looked healthy from every angle and simply never
answered.

Two things could leave it in that state and neither said anything. The loop
that listens for wake words could end on an unexpected error and nothing
restarted it or noticed. Separately, the flag that says "a voice turn is in
progress" could be left on after something went wrong mid-turn, and while it
is on the device deliberately ignores the microphone.

Both now recover on their own, and both write a loud error to the log first,
so a recurrence explains itself instead of looking like the device has gone
deaf for no reason. **If you saw this on 2.20.2-ea.4, please still send the
log if you have it** — the fix makes it survivable, but we do not yet know
what triggered it.

Nothing else changed. No database migration, no firmware requirement, nothing
to do on your devices.

## 2.20.2-ea.4 (Early Access)

Four fixes. Two change what you hear, one unblocks Home Assistant users who
could not administer the add-on, and one is a change to the dashboard's
speaker controls.

### Long answers no longer interrupt themselves

Ask for something lengthy — a story, a detailed explanation — and EchoMuse
would stop partway, sit with the ring lit, and then end without finishing.
It was hearing its own voice as a wake word and cancelling itself.

The detector that listens for you interrupting a response used to act on a
single 80ms fragment, at a deliberately low confidence bar. That was fine for
short replies, which never got near it, and wrong for long ones: continuous
speech offers far more chances to sound briefly like a wake word. Measured on
a real device, a short reply peaked at 0.03 while a story reached 0.18 against
a 0.05 bar.

Two frames in a row are now required, and the default bar has moved from 0.05
to 0.25. Interrupting still works — talking over a response scores far higher
than the response itself does. **If you had raised this setting yourself to
stop responses cutting out, you can put it back to the default.**

### The speaker settings are now one switch

The bass guard and limiter were five controls. None of them could be judged by
ear: the two stages cancel out each other's most obvious effect, and the depth
slider moves the overall level by 0.14dB across its entire range. They are now
a single **Speaker protection** toggle under Advanced, in the Playback section,
and it should be left on.

Nothing is lost — every setting still exists and still applies, and anything
you had set is still in force. If you tuned the limiter ceiling or the guard
depth, those values are unchanged; the controls to change them again have gone
from the dashboard.

The bass guard's default depth also moved from −20dB to −30dB. You will not
hear the difference, and it is not meant to be heard: it puts the default in
the middle of the range so there is room to adjust either way.

### Home Assistant users could be locked out of administering the add-on

If you set up a local EchoMuse account before opening the panel through Home
Assistant — including by copying your existing `/data` across when moving from
the container, which the migration guide tells you to do — every Home
Assistant user was created read-only, permanently. The only way back was
editing the database by hand.

The rule was counting accounts rather than accounts that can actually sign in
through Home Assistant, and a local password account cannot: the panel signs
you in through Home Assistant before it ever offers a login form. Fixed, and
the dashboard now takes your role from the server on load rather than
remembering it, so a role that is corrected or promoted takes effect on the
next page load instead of the next sign-in.

**If you are currently stuck read-only, updating should be enough.** No
database editing and no cache clearing.

### Speaker processing now says what it is doing

The add-on log records what the output chain is set to, whenever a stream
starts and whenever you change a setting, and how much work each stage
actually did. This is diagnostic only and changes nothing about playback — it
exists because "is this setting doing anything?" was not answerable from
outside, which cost four separate listening tests.

## 2.20.2-ea.3 (Early Access)

One fix, and it is the one that makes the previous release's headline feature
actually work.

### Speaker settings now take effect when you save them

Saving a limiter or bass guard setting updated the stored configuration but
never reached the running controller — those settings are applied by the
controller rather than by the Echo, and only a device reconnect picked them
up. So they appeared to do nothing, and would then quietly start working after
a restart, which is the point at which most people would stop investigating.

If you tried tuning the speaker and heard no difference, that was correct: the
settings were not reaching the audio. Together with the live updating added in
2.20.2-ea.2, moving the bass guard depth or the limiter ceiling is now audible
immediately, on the track you are already playing.

Worth re-doing any tuning you did before this release — you were listening to
unchanged sound.

## 2.20.2-ea.2 (Early Access)

Two changes, both about being able to tell what your system is doing. No new
database migration, no firmware requirement.

### Speaker settings now change while the music is playing

The limiter and bass guard settings only took effect when the next track
started, so changing one while listening appeared to do nothing at all. That
made them very hard to judge: by the time a new track began, the sound you
were comparing against was gone.

They now apply immediately, mid-track. Turning the bass guard on or off, or
moving its depth, is audible straight away and does not click or interrupt
playback. The equaliser behaves the same way.

If you have been trying to tune the speaker and concluded the settings made no
difference, this is why — it is worth another listen.

### Early Access uses its own ports

The Early Access and stable add-ons keep completely separate data, including
their own list of devices — but both handed out the same port numbers to the
Home Assistant satellites they create. Switching between them could therefore
leave Home Assistant talking to the wrong device, which showed up as devices
sitting unavailable, wake words lighting the ring, and every request ending
immediately with no answer.

Early Access now uses a separate range, so a leftover Home Assistant entry
from the other channel simply shows as unavailable instead of reaching
something unexpected. Existing devices keep the ports they already have;
nothing is renumbered.

**Switching channels is still a migration.** Each add-on has its own database
and its own certificate authority, so your devices will not connect to the
other channel until you copy `/data` across — see the Documentation tab.

## 2.20.2-ea.1 (Early Access)

Two fixes that stop the controller doing something wrong, and the first two
steps of making the speaker sound better. No new database migration, no
firmware requirement.

### Home Assistant sign-in through the sidebar works again

Opening EchoMuse from the Home Assistant sidebar failed and fell back to the
setup-token page. The lookup that matches your Home Assistant account to an
EchoMuse one crashed on every request, so automatic sign-in — the main reason
to run the add-on rather than the standalone container — has been unusable for
several releases. Reported and diagnosed by @lennart24, fixed by @finik.

### Wake words no longer fire at nobody

The wake-word engine fills its buffer with random noise whenever it is reset,
and we scored that noise as if it were sound for the next 1.28 seconds. On one
device in one day that produced **19 wake events with nobody speaking**, all of
them just under a second after a turn ended, some scoring higher than real
speech does. Worse, it could fire the barge-in detector and cancel an answer
you were waiting for.

Raising the wake-word threshold would not have helped — these score higher than
real speech, and two of the recorded events cleared 0.80. Found, measured and
fixed by @dmndru.

One consequence worth knowing: interrupting a response is now ignored for the
first 1.28 seconds of it. That is the right trade against turns being cancelled
by nobody.

### The equalizer no longer distorts what it boosts

Turning any EQ band up could push the audio past full scale, and it was being
hard-clipped — measured at **4.7% of samples** on a normal signal with a modest
bass boost, and 18% at the top of the sliders. Flat EQ was unaffected, so this
only ever hit people who reached for the controls to improve their sound.

There is now a limiter on the output. Same test signal, same settings, no
clipping at all, for 1–6 dB of gain reduction rather than the 12 dB a simple
volume trim would have cost. **Limiter** and its ceiling and release are in
Playback; leave it on.

### New: Bass guard

A dynamic control that drops bass the speaker physically cannot produce.

It sounds backwards, and it is the single biggest thing available for a driver
this size. Frequencies below about 115 Hz still move the cone even though you
cannot hear them, and that movement muddies everything above — which is what
"tin-can" actually is. Removing them makes the midrange clearer, and measurably
*louder*, because the limiter no longer has to hold the whole signal down to
contain bass peaks nobody was going to hear. Quiet passages keep their low end;
only loud content is affected.

The parameters come from measurements of the stock Amazon firmware on the same
speaker. **Bass guard depth** is in Playback, defaulting to −20 dB where stock
uses −40 dB — deliberately gentler, because stock pairs its setting with an
equalizer curve we have not measured.

**This one wants your ears.** Play something with real low end and try the depth
from 0 to −40. Expect it to sound thinner at first and then clearer; the best
setting is probably deeper than feels right.

### Also

- Every device now carries all four stock wake word models, so changing wake
  word no longer requires a device to have been provisioned with it.
- Security updates to the web server, TLS and mDNS libraries.
- The rooting guide now says the unlock needs Linux, not macOS. Thanks
  @StefanOltmann.

## 2.20.1

Changes since the 2.20.0 prerelease builds. All fixes — no new settings,
no database migration, no firmware requirement, and nothing to do on your
devices.

### Interrupting a response now works

Saying the wake word while EchoMuse was answering stopped it — and then
nothing happened. Whatever you said next was never heard, so you had to wait
and ask again. The interrupting turn was ending a few milliseconds after it
began, before any audio reached it.

Home Assistant runs one voice pipeline at a time for each device, and EchoMuse
was starting a second one without telling it to stop the first; the abandoned
pipeline's ending then arrived and closed the new turn. Fixed for both cases —
interrupting while it is thinking, and while it is speaking.

Interrupting is still a one-way door: say the wake word and then stay quiet,
and the original answer is gone rather than resumed.

### Announcements

- **They now finish before Home Assistant is told they have.**
  `assist_satellite.announce` returned the moment the request arrived rather
  than when the audio stopped, so an automation playing two announcements in a
  row could have them talk over each other, and the satellite dropped out of
  its "responding" state early. An announcement now also reports whether the
  audio actually reached the speaker.
- **A cancelled voice turn no longer silences every announcement after it.**
  Press the action button to stop a turn, or mute the device, and
  announcements stopped playing there — no error, nothing in the dashboard,
  and it stayed that way until a voice turn happened to run. With more than
  one device this looked like announcements moving to the wrong device.
  Long-standing, and only found by trying enough announcements in a row.

### Wake words

- **Changing a wake word installs it before switching the device onto it.**
  If you picked a wake word a device had never been given, and that device was
  doing its own wake word detection, it stopped answering entirely — it had no
  model to listen with, and nothing said so while the dashboard showed the
  device as healthy. The device now keeps listening for its current wake word
  until the new model has arrived, then switches. If the model cannot be
  installed it stays where it is and the device log says why.

  Devices using controller-side detection are unaffected — the model file
  means nothing to them, so the change stays immediate.

  Note that changing a wake word briefly reconnects the device in Home
  Assistant, which makes every entity for it flicker unavailable and back. If
  you have an automation with a **state trigger** on one of them it will fire.
  See the configuration guide under *Wake word model*.

### Elsewhere

- **The dashboard shows Speaking again.** A device tile sat on "Thinking" for
  the whole spoken response. It now follows the device's own report that the
  audio has stopped, rather than the moment the controller finished sending it
  — which happens almost instantly and left several seconds of audio still to
  play. The start of a response is still estimated and can lead the speaker by
  about a second.
- **Firmware is downloaded once per release, not once per device.** Updating
  several devices, or setting up several through the provisioning wizard,
  re-downloaded the same ~10MB every time. It is now kept beside your database
  and checked against its fingerprint before use.
- **A guide for moving from the Docker container to the Home Assistant
  add-on**, in `docs/migrate-to-addon.md`. Read the part about `tls/` before
  you start.

## 2.20.1-ea.5 — Prerelease

- **Changing a wake word now installs it before switching the device onto
  it.** In ea.3 and ea.4, picking a wake word a device did not have made the
  controller take over the listening for that device while it installed the
  model — even if you had deliberately chosen on-device detection. It worked,
  but it quietly changed a setting you had chosen and the dashboard carried on
  showing the setting you asked for.

  The device now keeps listening for its current wake word, on the device,
  until the new model has actually arrived — then it switches. If the model
  cannot be installed, the device stays on the wake word it already has and
  the device log says so. Nothing you chose gets overridden.

  Devices that use controller-side wake word detection are unaffected: the
  model file means nothing to them, so the change applies immediately as
  before.

## 2.20.1-ea.4 — Prerelease

- **The dashboard's Speaking state now follows the device.** In ea.3 the tile
  showed Speaking slightly early, dropped back to Thinking while the device
  was still talking, then went idle. It was following when the controller
  finished *sending* audio, which happens almost instantly — the device still
  had several seconds of it left to play. It now waits for the device to
  report that the audio has actually stopped.

  The start of a response is still estimated and can lead the speaker by about
  a second: the device deliberately holds audio until it has enough buffered,
  and it only tells the controller when playback *ends*. Reporting the start
  too needs a firmware change.

## 2.20.1-ea.3 — Prerelease

- **Changing a device's wake word can no longer leave it deaf.** If you picked
  a wake word a device had never been given, and that device was doing its own
  wake word detection, it stopped answering entirely — it had no model to
  listen with, and the controller had already stepped back from listening on
  its behalf. Nothing said so, and the dashboard showed the device as healthy.
  The controller now keeps listening for that device while it installs the
  model, and stays listening if the install fails.

- **The dashboard shows Speaking again.** A device tile sat on "Thinking" for
  the whole spoken response. The tile was only ever told about listening,
  thinking and the end of the turn, so it never heard about the bit in
  between.

- **Firmware is downloaded once per release, not once per device.** Updating
  several devices, or setting up several through the wizard, re-downloaded the
  same ~10MB every time. It is now kept on disk beside your database and
  checked against its fingerprint before use.

## 2.20.1-ea.2 — Prerelease

Two announcement faults found while testing ea.1.

- **A cancelled voice turn used to silence every announcement after it.**
  Press the action button to stop a turn — or mute the device — and
  announcements stopped playing on that device, with no error and nothing in
  the dashboard to show for it. It stayed that way until a voice turn happened
  to run. Long-standing; ea.1's announcement changes are simply what made
  anyone try enough announcements in a row to find it. If you have more than
  one device, this looked like announcements moving to the wrong device: the
  others were fine, because only the cancelled one was affected.

- **`media_player.play_media` with announce turned on failed outright in
  ea.1.** A rename in that release missed this second announcement path, so
  every call raised an internal error and nothing played.
  `assist_satellite.announce` was unaffected.

An announcement now also tells Home Assistant whether the audio actually
reached the speaker, rather than always reporting success.

## 2.20.1-ea.1 — Prerelease

- **Talking over a response now works.** Saying the wake word while EchoMuse
  was answering did stop it — and then nothing happened. Whatever you said
  next was never heard: the interrupting turn ended a few milliseconds after
  it began, before any audio was captured, so you had to wait and start again.
  Home Assistant runs one voice pipeline at a time per device, and EchoMuse
  was starting a second without telling it to stop the first; the old
  pipeline's ending then arrived and closed the new turn. Fixed for both
  cases — interrupting while it is thinking, and while it is speaking.

  Note that interrupting is still a one-way door. If you say the wake word and
  then stay quiet, the original answer is gone rather than resumed.

- **Announcements now finish before Home Assistant is told they have.** The
  `assist_satellite.announce` service returned the moment the request
  arrived rather than when the audio stopped. An automation that plays two
  announcements in a row could have them talk over each other on the device,
  and the satellite dropped out of its "responding" state early. It now waits
  for playback, and says whether the audio actually reached the speaker.

## 2.20.0-ea.6 — Prerelease

An Early Access build on top of 2.20.0. The 2.20.0 notes below are the
release this builds on — you already have them if you were running an
earlier Early Access build.

- **Installing wake word models on a device works again.** Sending a wake
  word model to an Echo failed with an internal error, every time. If you
  changed a device's wake word to one it had not been given during setup, it
  simply stopped responding — it had no model to listen with, and nothing
  said so. Fixed, with a test so it cannot come back quietly.

## 2.20.0

Everything since 2.19.0. Two changes need a moment of your attention — the
wake word name and the volume range — and both are called out below.

### Adding a device in Home Assistant now works

Adding an EchoMuse device opened a dialog asking you to say the wake word,
and it never advanced: the ring lit, the device answered, and **Skip** was
the only way on. Home Assistant listens for the *name* of the wake word that
fired and the controller never sent one. Every ordinary voice turn already
worked, which is why this went unnoticed for so long — the setup dialog is
the only thing that reads it.

Two follow-on fixes came out of the same flow. The device now stops
listening the moment Home Assistant is finished, instead of holding the
microphone until a timeout — which is what made the dialog's second prompt
land on a device still busy with the first, and what caused the occasional
"device not found, press Retry". And the ring holds briefly so you can see
it heard you, rather than flashing for a tenth of a second.

### ⚠️ Your wake word may need selecting again

The wake word reported to Home Assistant loses its version suffix: **"hey
jarvis v0.1" becomes "hey jarvis"**. The `_v0.1` is an openWakeWord filename
convention that was never meant to be read by a person, and it had been
reaching Home Assistant's own device registry.

Home Assistant restores a wake word choice **by name**, so after updating, a
device may show none selected. Set it again under Settings → Devices &
Services. One dropdown, once per device.

### ⚠️ Volume distorted above about three-quarters, and no longer does

EchoMuse was driving the codec's digital volume past the point where it can
only clip — measured at **65% distortion three button presses above the
midpoint, and 89% at the maximum**, with the output no longer getting any
louder. Stock Alexa never touches that control. This is the substance of
every report that EchoMuse sounded worse than stock when turned up. Found
and measured by @kdkavanagh.

Two things you will notice:

- **The volume percentage in Home Assistant will read higher** for a device
  sitting at the same physical level, because the scale no longer includes a
  stretch that only distorted. Nothing got louder or quieter. **If you have
  an automation with a volume threshold in it, check that threshold.**
- **The top of the range is quieter than it was.** What used to be above it
  was a square wave, so this is the fix rather than a regression — but if
  you regularly ran a device near maximum you will hear it. Cleaner, and not
  as loud.

The physical buttons now step about 4dB per press across the audible range,
instead of spending presses near the bottom of a scale where nothing is
audible — silencing a device is the mute button's job. The volume ring spans
that same range, so a press always moves it.

### Home Assistant add-on

- **Leaving "Server IP" empty now detects this host's LAN address.** The old
  fallback was a hardcoded address, so a fresh install with the field blank
  advertised it to every device over mDNS: the controller ran perfectly and
  no device could reach it, with nothing reporting an error anywhere. If you
  set the field by hand to work around that, you can clear it.
- **No separate dashboard login under Home Assistant.** HA has already
  authenticated you, so the panel signs you in as your HA user and the
  first-run setup token is gone. The first person through becomes admin;
  everyone after is read-only until promoted. There is no Sign out on an HA
  session — sign out of Home Assistant instead.
- **Recordings and transcripts are admin-only.** Saved utterances are
  recognisable speech from inside your home, and every household HA user can
  reach the panel, so read-only accounts no longer see the audio player or
  the transcript text. Enforced on the server, not hidden in the page.
- **A Debug logging option.** The controller could always do this, but only
  if you ran it as a container and knew the environment variable. Leave it
  off unless you are chasing something; it is verbose.
- The provisioning wizard's WebUSB error now names the exact origin the
  browser needs allowed, instead of suggesting an address the add-on refuses.

### Elsewhere

- **Entity names no longer repeat the device name.** Home Assistant already
  puts the device name in front of every entity and we were adding it again,
  so a sensor read "Kitchen Voice Assistant Kitchen Ambient Light". Only the
  displayed name changes: entity IDs stay as they are, so automations keep
  working, and an entity you renamed yourself keeps your name.
- **Greyed-out switches no longer do the opposite of what they show.** A
  disabled toggle could still be clicked, and stored the inverted value.
  Thanks to @kdkavanagh.
- Roles can be changed via `PATCH /api/users/{id}`; the last admin cannot be
  demoted.
- A voice turn that Home Assistant ends before it starts listening is no
  longer recorded as "no speech" — it was blaming the speaker for something
  at the other end, in a figure the activity statistics report.
### Prerelease builds

This release was preceded by `2.20.0-ea.1` through `2.20.0-ea.5`. Each carried
a subset of the above; `2.20.0-ea.5` is the same set of changes as this
release.


## 2.19.0

- **EchoMuse can be installed as a Home Assistant add-on** rather than run
  with docker compose by hand. The dashboard appears as a sidebar panel
  through ingress, so it is reachable wherever Home Assistant is without
  exposing another port. Thanks to @natecj for building it, and @Pinball3D
  whose earlier attempt worked out the approach.
- Existing docker compose installs are unaffected.
- **Six shipped defaults corrected — new installs only.** Wake threshold
  0.3 → 0.5, beamforming, echo cancellation and barge-in on by default,
  barge-in threshold 0.10 → 0.05. Stored configuration always wins over a
  shipped default, so an existing controller keeps every value it has.

## 1.0.1

- Initial Home Assistant Supervisor add-on packaging: install and run the
  controller from Settings → Add-ons instead of hand-run docker-compose.
- Ingress support for the dashboard (no separate port to expose).
- Add-on config UI labels, icon, and logo.
