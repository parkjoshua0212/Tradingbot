import logging
import sys

from kis_client import KISClient

logging.basicConfig(level=logging.INFO)
c = KISClient()
c.token()
print("token ok")
print("005930 price:", c.get_price("005930"))
b = c.get_balance()
print(f"cash {b['cash']:,}  equity {b['equity']:,}")
for t, p in b["positions"].items():
    print(f"  {t} {p['name']}: {p['qty']} @ {p['avg_price']:,.0f}")
if "--raw" in sys.argv:  # dump the balance summary to check which field is the cash we want
    print(c._balance_raw()["output2"][0])
