import unittest
from decimal import Decimal
from exchanges import binance_closed_hours
from shortbot import select_alerts


class BinanceSignal(unittest.TestCase):
    def test_decision_uses_same_hour_low_and_high_and_only_latest_24_volumes(self):
        candles = [[i*3600000, '1', '1', '0.001', '1', '0', 0, '1'] for i in range(25)]
        candles[0][3] = '0.031820'
        candles[0][7] = '999999999'
        def get(url):
            if 'exchangeInfo' in url:
                market = dict(contractType='PERPETUAL', status='TRADING', symbol='ACEUSDT',
                              quoteAsset='USDT', baseAsset='ACE')
                return {'symbols': [market] if 'fapi' in url else []}
            self.assertIn('startTime=0&endTime=89999999&limit=25', url)
            return candles
        for high, volume, expected in [('0.087610', 24, True), ('0.063640', 24, True),
                                       ('0.063639', 24, False), ('0.087610', 25, False)]:
            candles[-1][2] = high
            rows = binance_closed_hours(90000, ('USDT',), get)
            alerts = select_alerts(rows, exchange='binance', volume_threshold=Decimal(volume))
            self.assertEqual(bool(alerts), expected)
            if alerts and high == '0.087610':
                self.assertEqual(round(alerts[0].increase, 2), Decimal('175.33'))
