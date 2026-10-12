/**
 * Python's `round(x, ndigits)` for floats.
 *
 * CPython rounds the exact binary value of `x` to `ndigits` decimals, sending
 * exact ties to even, and returns the double nearest that decimal. The usual JS
 * idiom `Math.round(x * 10 ** n) / 10 ** n` differs twice over: the
 * multiplication rounds before the rounding does (2.675 -> 2.68, Python 2.67),
 * and Math.round sends ties up (0.5 -> 1, Python 0).
 *
 * This works on the exact value: a double is mantissa * 2^exponent, so
 * x * 10^n is an exact rational that BigInt can divide without error.
 */
export function pyRound(x: number, ndigits: number): number {
  if (!Number.isInteger(ndigits) || ndigits < 0) {
    throw new RangeError(`ndigits must be a non-negative integer, got ${String(ndigits)}`);
  }
  if (!Number.isFinite(x) || x === 0) return x;

  const { mantissa, exponent } = decompose(Math.abs(x));
  const scale = 10n ** BigInt(ndigits);

  let quotient: bigint;
  if (exponent >= 0) {
    quotient = (mantissa << BigInt(exponent)) * scale;
  } else {
    const numerator = mantissa * scale;
    const denominator = 1n << BigInt(-exponent);
    quotient = numerator / denominator;
    const twiceRemainder = (numerator % denominator) * 2n;
    if (twiceRemainder > denominator || (twiceRemainder === denominator && quotient % 2n === 1n)) {
      quotient += 1n;
    }
  }

  // Parsing the decimal string gives the nearest double, as CPython's strtod does.
  const magnitude = Number(`${quotient.toString()}e-${String(ndigits)}`);
  return x < 0 ? -magnitude : magnitude;
}

/** |x| as mantissa * 2^exponent, exactly. */
function decompose(x: number): { mantissa: bigint; exponent: number } {
  const view = new DataView(new ArrayBuffer(8));
  view.setFloat64(0, x);
  const bits = view.getBigUint64(0);
  const biasedExponent = Number((bits >> 52n) & 0x7ffn);
  const fraction = bits & 0xfffffffffffffn;
  if (biasedExponent === 0) {
    return { mantissa: fraction, exponent: -1074 }; // subnormal
  }
  return { mantissa: fraction | (1n << 52n), exponent: biasedExponent - 1075 };
}
