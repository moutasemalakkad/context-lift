# The README's animated demo. Regenerate with `make demo-gif`.
# The Makefile crops to the measured result; current next-step commands live
# in the quickstart rather than in the recorded program's footer.
#
# It records `contextlift demo`: the whole loop on a 7-row warehouse, with
# canned model answers, so it runs with no key, no network and no cloud. The
# output says that on screen. Everything else in it -- the SQL execution, the
# paired test, the interval, the verdict -- is the real code.
#
# Needs: brew install vhs ttyd ffmpeg

Output /tmp/contextlift-demo-full.gif

Require contextlift

Set Shell bash
Set FontSize 14
Set Width 960
Set Height 700
Set Padding 18
Set Theme "Catppuccin Mocha"
Set Framerate 24
Set TypingSpeed 60ms

# Set a bare prompt and leave the home path out of the recording.
Hide
Type "export PS1='$ ' && clear"
Enter
Show

Sleep 600ms
Type "contextlift --workspace /tmp/contextlift-demo demo"
Sleep 400ms
Enter
Sleep 7s
