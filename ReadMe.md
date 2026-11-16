# TubeForge Studio Pro — Windows Quick-Start Guide

## 1. How to Start the Application (Daily Use)
Whenever you restart your PC or open a new terminal window, run these 3 commands in **PowerShell** or **Command Prompt (CMD)**:

```powershell
cd "C:\Users\Ravi Ranjan\Downloads\turbo"
.\venv\Scripts\activate
streamlit run app.py
```

## Free-tier production mode

The app opens the Cinematic AI Studio, which limits text generation to Groq and explicitly free OpenRouter models,
images to Cloudflare FLUX.2 Klein 4B (with a local 10,000-neuron daily reservation), anonymous Pollinations, and
Pixabay, and narration to Microsoft Edge TTS. Music, sound effects, motion, and rendering are produced locally.
Paid hosted video generation and the legacy Gemini-based Classic Studio are disabled. Original motion uses a
generated still with local camera/parallax animation; it is not semantic AI-generated character motion.

Free-service quotas, availability, and terms still apply. The Cloudflare reservation covers this app's usage only;
other workloads using the same account can consume its daily allowance, so use a separate free-tier account with
billing disabled if you need to avoid unexpected charges.