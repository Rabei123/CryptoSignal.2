def detect_bullish_patterns(df):
    patterns = []

    # Check for Hammer (bullish reversal pattern)
    hammer = (df['close'] > df['open']) & \
             (df['high'] - df['close'] > 2 * (df['close'] - df['open'])) & \
             (df['high'] - df['low'] > 3 * (df['close'] - df['open']))
    if hammer.iloc[-1]:
        patterns.append("Hammer")

    # Check for Engulfing (bullish engulfing pattern)
    engulfing = (df['close'] > df['open'].shift(1)) & \
                (df['open'] < df['close'].shift(1)) & \
                (df['close'] > df['open'])
    if engulfing.iloc[-1]:
        patterns.append("Engulfing")

    # Check for Morning Star
    morning_star = (df['close'].iloc[-3] < df['open'].iloc[-3]) & \
                   (df['open'].iloc[-2] < df['close'].iloc[-2]) & \
                   (df['close'].iloc[-1] > df['open'].iloc[-1])
    if morning_star:
        patterns.append("Morning Star")

    return patterns
