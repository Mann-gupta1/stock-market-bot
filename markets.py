"""Market config. cost = swing per-side guess, icost = intraday per-side guess (fees+taxes, excl. slippage & FX).
min_tv = min avg daily traded value in the quote unit (UK quotes in pence). Verify costs with your broker."""

MARKETS = {
    "IN": dict(sfx=".NS", index="^NSEI", tz="Asia/Kolkata", open="09:15", close="15:30", ccy="INR",
               cost=0.0015, icost=0.0004, min_tv=10e7, tickers="""RELIANCE TCS HDFCBANK INFY ICICIBANK HINDUNILVR
               ITC SBIN BHARTIARTL KOTAKBANK LT AXISBANK ASIANPAINT MARUTI SUNPHARMA TITAN ULTRACEMCO BAJFINANCE
               NESTLEIND WIPRO HCLTECH M&M NTPC POWERGRID TATASTEEL JSWSTEEL ONGC COALINDIA ADANIPORTS TECHM
               INDUSINDBK BAJAJFINSV DRREDDY CIPLA EICHERMOT GRASIM HINDALCO HEROMOTOCO BRITANNIA APOLLOHOSP BPCL
               SBILIFE HDFCLIFE TRENT BEL"""),
    "JP": dict(sfx=".T", index="^N225", tz="Asia/Tokyo", open="09:00", close="15:30", ccy="JPY",
               cost=0.0010, icost=0.0005, min_tv=2e9, tickers="""7203 6758 9984 8306 6861 9432 8035 6501 4063 9983
               6098 8316 7974 4502 6367 8058 8001 6902 7267 4568 6857 9433 6594 8766 7741"""),
    "UK": dict(sfx=".L", index="^FTSE", tz="Europe/London", open="08:00", close="16:30", ccy="GBp",
               cost=0.0030, icost=0.0027, min_tv=1e9, tickers="""AZN SHEL HSBA ULVR BP RIO GSK REL DGE BATS LSEG
               BARC LLOY NWG RR BA AAL GLEN NG VOD TSCO PRU CPG EXPN"""),
    "US": dict(sfx="", index="^GSPC", tz="America/New_York", open="09:30", close="16:00", ccy="USD",
               cost=0.0005, icost=0.0001, min_tv=50e6, tickers="""AAPL MSFT NVDA AMZN GOOGL META TSLA AVGO JPM V LLY
               UNH XOM MA COST HD PG NFLX AMD CRM ORCL ADBE WMT BAC KO PEP MRK ABBV CVX INTC"""),
    "HK": dict(sfx=".HK", index="^HSI", tz="Asia/Hong_Kong", open="09:30", close="16:00", ccy="HKD",
               cost=0.0015, icost=0.0012, min_tv=100e6, tickers="""0700 9988 0005 1299 0941 3690 0388 2318 0939 1398
               0883 1810 2020 0001 0016 1211 9618 0027 0002"""),
    "AU": dict(sfx=".AX", index="^AXJO", tz="Australia/Sydney", open="10:00", close="16:00", ccy="AUD",
               cost=0.0010, icost=0.0008, min_tv=20e6, tickers="""BHP CBA CSL NAB WBC ANZ WES MQG GMG FMG WDS TLS RIO
               WOW ALL TCL COL STO REA QBE"""),
    "DE": dict(sfx=".DE", index="^GDAXI", tz="Europe/Berlin", open="09:00", close="17:30", ccy="EUR",
               cost=0.0010, icost=0.0005, min_tv=20e6, tickers="""SAP SIE ALV DTE MBG BMW BAS BAYN MUV2 IFX ADS DBK
               VOW3 DHL RWE EOAN DB1 HEN3 BEI MRK"""),
    "FR": dict(sfx=".PA", index="^FCHI", tz="Europe/Paris", open="09:00", close="17:30", ccy="EUR",
               cost=0.0025, icost=0.0005, min_tv=20e6, tickers="""MC OR TTE SAN AIR SU AI BNP RMS EL CS DG KER SAF BN
               ORA GLE CAP RI VIE"""),
    "CA": dict(sfx=".TO", index="^GSPTSE", tz="America/Toronto", open="09:30", close="16:00", ccy="CAD",
               cost=0.0010, icost=0.0005, min_tv=20e6, tickers="""RY TD SHOP ENB CNR CP BMO BNS CNQ SU TRP ATD MFC BCE
               CM NTR WCN CSU FTS L"""),
}


def symbols(mkt):
    m = MARKETS[mkt]
    return [t + m["sfx"] for t in m["tickers"].split()]


def inr_rate(ccy):
    """INR per 1 quote unit. GBp = pence."""
    import yfinance as yf
    if ccy == "INR":
        return 1.0
    base = "GBP" if ccy == "GBp" else ccy
    rate = float(yf.Ticker(f"{base}INR=X").history(period="5d").Close.dropna().iloc[-1])
    return rate / 100 if ccy == "GBp" else rate
