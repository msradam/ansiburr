# VHS tape for the plan-then-apply demo. Three scenarios:
# 1. Small diff under policy -> approve -> apply.
# 2. Oversized diff -> review rejects, no apply runs.
# 3. Re-run with default -> idempotent.
#
# Renders to vhs/plan_then_apply.gif.

Output vhs/plan_then_apply.gif

Set FontSize 14
Set Width 1280
Set Height 720
Set Theme "Catppuccin Mocha"
Set TypingSpeed 25ms
Set PlaybackSpeed 1.0

Type "# Scenario 1: small diff -> review approves -> applied -> verified"
Enter
Sleep 600ms
Type "uv run python examples/plan_then_apply/fsm.py"
Sleep 300ms
Enter
Sleep 6s

Type "# Scenario 2: oversized diff -> review rejects, no apply runs"
Enter
Sleep 600ms
Type 'NEW_MESSAGE="$(python3 -c "print(chr(120)*500)")" uv run python examples/plan_then_apply/fsm.py'
Sleep 300ms
Enter
Sleep 6s

Type "# Re-run: idempotent"
Enter
Sleep 600ms
Type "uv run python examples/plan_then_apply/fsm.py"
Sleep 300ms
Enter
Sleep 6s
