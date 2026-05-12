@echo off
:: Set your Anthropic API key here (or set it in your environment beforehand)
:: set ANTHROPIC_API_KEY=sk-ant-YOUR_KEY_HERE

python -m streamlit run "%~dp0stock_dashboard.py"
pause
