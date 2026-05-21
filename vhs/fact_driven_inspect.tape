# VHS tape for the fact-driven inspection demo.
# Renders to vhs/fact_driven_inspect.gif.

Output vhs/fact_driven_inspect.gif

Set FontSize 16
Set Width 1200
Set Height 600
Set Theme "Catppuccin Mocha"
Set TypingSpeed 30ms
Set PlaybackSpeed 1.0

# Run the demo. The FSM gathers facts from the container, branches on
# ansible_pkg_mgr, then runs the apt-specific inspection.
Type "uv run python examples/fact_driven_inspect/fsm.py"
Sleep 400ms
Enter
Sleep 5s
