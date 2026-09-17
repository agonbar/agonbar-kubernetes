#!/usr/bin/env python3
"""Compara amortizar anticipadamente el préstamo del coche (Cetelem) contra
pagarlo entero el día de la firma de la casa.

Condiciones del contrato (28-mar-2025, 39.520,75 € a 84 meses, TIN 4,99%,
TAE 6,38%): el reembolso anticipado parcial se puede aplicar a reducir la cuota
o el plazo, PERO solo si llega a tres mensualidades (1.675,20 €); por debajo de
eso siempre reduce plazo, que es además lo que se aplica por defecto. La
compensación es como mucho el 1% de lo reembolsado, o el 0,5% si queda menos de
un año. La Ley 16/2011 exime de compensación si lo amortizado en 12 meses no
pasa de 1.000 €.

    scripts/cetelem-amortizacion.py [--meses 38] [--extra 400]
"""
import argparse

TIN = 4.99 / 100 / 12
CUOTA = 558.40
SALDO_HOY = 32_594.19          # a 17-sep-2026
MINIMO_PARA_ELEGIR = 3 * CUOTA  # 1.675,20
COMISION = 0.01


def simula(extra_mes=0.0, modo="plazo", meses=38):
    saldo, cuota, pagado, bote = SALDO_HOY, CUOTA, 0.0, 0.0
    fin = None
    for mes in range(1, meses + 1):
        if saldo <= 0:
            fin = fin or mes
            continue
        interes = saldo * TIN
        saldo -= min(cuota - interes, saldo)
        pagado += cuota
        bote += extra_mes
        if bote >= MINIMO_PARA_ELEGIR and saldo > 0:
            amortizado = min(bote, saldo)
            saldo -= amortizado
            pagado += amortizado * (1 + COMISION)
            bote = 0.0
            if modo == "cuota" and saldo > 0:
                # se mantienen los meses que quedaban, baja el importe
                n, s = 0, saldo
                while s > 0 and n < 600:
                    s -= cuota - s * TIN
                    n += 1
                cuota = saldo * TIN * (1 + TIN) ** n / ((1 + TIN) ** n - 1)
        if saldo <= 0:
            fin = mes
    return saldo, pagado, cuota, fin


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--meses", type=int, default=38, help="mes de la firma de la casa")
    args = ap.parse_args()

    saldo, pagado, _, _ = simula(0, meses=args.meses)
    base = pagado + saldo * (1 + COMISION)
    print(f"sin amortizar: {args.meses} cuotas ({pagado:,.0f}) + cancelar {saldo*(1+COMISION):,.0f} = {base:,.0f}\n")
    print(f"{'extra/mes':>10} {'modo':>7} {'cuota final':>12} {'saldo a la firma':>17} {'acaba en':>9} {'total':>10} {'ahorro':>8}")
    for extra in (100, 200, 400, CUOTA):
        for modo in ("plazo", "cuota"):
            saldo, pagado, cuota, fin = simula(extra, modo, args.meses)
            total = pagado + saldo * (1 + COMISION)
            print(f"{extra:>10,.0f} {modo:>7} {cuota:>12,.0f} {saldo:>17,.0f} "
                  f"{(str(fin) if fin else '-'):>9} {total:>10,.0f} {base-total:>8,.0f}")


if __name__ == "__main__":
    main()
