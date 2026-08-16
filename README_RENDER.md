# SET100 Flow Dashboard

Separate Flask dashboard for SET100. It does not share files or browser storage keys with the old SET50 app.

## Pages

- `/flow` - SET100 Money Flow TOP10
- `/paper` - Paper Trade with four portfolios:
  - `30avg-top5flow`
  - `30avg-top10flow`
  - `avg12-top5flow`
  - `avg12-top10flow`

Initial capital is 5,000,000 baht. Top 5 portfolios use 1,000,000 baht per position. Top 10 portfolios use 500,000 baht per position.

## Local run

Run `run_dashboard.bat`, then open:

- `http://127.0.0.1:5001/flow`
- `http://127.0.0.1:5001/paper`

## Ticker universe

The app reads SET100 symbols from `set100_tickers.txt`. Update that file when SET changes the constituent list.

## Render settings

If creating a Web Service manually:

- Runtime: Python
- Build command: `pip install -r requirements.txt`
- Start command: `gunicorn app:app`
- Plan: Free

## URLs after deploy

Render will give a URL like:

- `https://your-service-name.onrender.com/flow`
- `https://your-service-name.onrender.com/paper`

## Notes

- The free plan may sleep when nobody uses it. First open can be slow.
- The dashboard fetches data from Yahoo Finance through `yfinance`, so refreshes may take time.
- If Yahoo rate-limits data, wait a few minutes and refresh again.
