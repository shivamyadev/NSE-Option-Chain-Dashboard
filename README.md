A live NSE Option Chain Analysis Dashboard built with Streamlit, Plotly, and nsepython.
It helps traders and analysts track real-time Open Interest (OI) data, percentage changes, and visual trends for CE (Call) and PE (Put) options — with automatic data saving and chart reloading.

⚡ Features
📊 Live Option Chain Data using nsepython
🔁 Auto-refresh and update charts every few seconds
💾 Local data saving — automatically stores fetched data as CSVs on your system
📈 Separate CE and PE charts for better clarity
🧮 % Change in OI instead of raw sums
🔔 Alert system for CE and PE thresholds
🕒 Choose between candle view and line view
🧭 View range options: 1H, 2H, 4H, 1D, 2D, 1W, 1M, All
📂 Auto-load previous data when reopening the same symbol

🧠 How It Works
Select your symbol (e.g., NIFTY, BANKNIFTY).
The app fetches the latest option chain from NSE.
It computes percentage changes in OI for CE and PE separately.
Charts update live using Plotly visuals.
The data is saved automatically in your system’s local folder.
When you open the app again, it auto-loads the saved history.
