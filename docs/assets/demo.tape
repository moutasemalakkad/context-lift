# The README's animated demo. Regenerate with `make demo-gif`.
#
# It records `contextlift demo`: the whole loop on a 7-row warehouse, with
# canned model answers, so it runs with no key, no network and no cloud. The
# output says that on screen. Everything else in it -- the SQL execution, the
# paired test, the interval, the verdict -- is the real code.
#
# Needs: brew install vhs ttyd ffmpeg

Output docs/assets/demo.gif

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
Type "contextlift demo"
Sleep 400ms
Enter
Sleep 7s
