import pandas as pd

import select_universe as su


def test_rule_order_and_finance_is_one_sector():
    rows = [  # ticker, name, sector, close, market cap, volume
        ("000010", "BankA", "은행", 50_000, 100, 1),
        ("000020", "BrokerB", "증권", 50_000, 90, 1),
        ("000030", "HoldingC", "기타금융", 50_000, 85, 1),   # third finance name: sector full
        ("000040", "InsurerD", "보험", 50_000, 84, 1),       # fourth: still the same sector
        ("000050", "ChipE", "전기·전자", 150_001, 80, 1),    # just over the price limit
        ("000055", "ChipE우", "전기·전자", 50_000, 70, 1),   # preferred
        ("000060", "Fund", "기타", 10_000, 60, 1),           # ETF
        ("000070", "Halted", "화학", 10_000, 50, 0),         # no volume
        ("000080", "ChemF", "화학", 150_000, 40, 1),         # exactly at the limit: kept
        ("000090", "ChemG", "화학", 10_000, 30, 1),
        ("000100", "ChemH", "화학", 10_000, 20, 1),          # third 화학: sector full
    ]
    df = pd.DataFrame(rows, columns=["ticker", "종목명", "업종명", "종가", "시가총액", "거래량"]).set_index("ticker")
    picks, notes = su.select(df.sample(frac=1, random_state=0), {"000060"})  # input order must not matter
    assert picks == ["000010", "000020", "000080", "000090"]
    why = {n.split()[0]: n for n in notes}
    assert "already has 2" in why["000030"] and "already has 2" in why["000040"] and "already has 2" in why["000100"]
    assert "> 150,000" in why["000050"] and "preferred" in why["000055"] and "ETF" in why["000060"] and "halted" in why["000070"]
    assert su.select(df, {"000060"}, n=2)[0] == ["000010", "000020"]  # stops at n


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print("ok", name)
