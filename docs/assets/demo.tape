# Regenerate docs/assets/demo.gif:   make demo   (runs `uv run vhs docs/assets/demo.tape`)
# Requires vhs and ttyd on PATH: https://github.com/charmbracelet/vhs#installation

Output docs/assets/demo.gif

Set Shell bash
Set FontSize 15
Set Width 1300
Set Height 900
Set Padding 20
Set Theme "Catppuccin Mocha"
Set TypingSpeed 35ms

Hide
# Make the mock LLM hallucinate on its first answer so the evaluator's correction is always visible.
Type "export MOCK_LLM_FAULT_RATE=1 COLUMNS=120 PS1='$ ' && clear" Enter
Sleep 500ms
Show

Type "resilio dev simulate --type memory-leak --local --auto-approve"
Sleep 600ms
Enter
Sleep 9s
