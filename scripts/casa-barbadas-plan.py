#!/usr/bin/env python3
"""Plan de compra de la casa de Barbadás.

Dos fases:
  1. Espera: se sigue pagando el alquiler actual y se ahorra, hasta poder
     entregar los 30.000 en mano que bajan la hipoteca a un importe que el
     banco conceda.
  2. Alquiler con opción a compra: 24 meses a 1.500 que descuentan del precio.
     Antes de firmar hay que liquidar el coche (el banco lo exige) y pagar
     impuestos y gastos.

Responde a "cuanto tardo en poder empezar" para cada nivel de ayuda de la
pareja. Cifras de ingresos y gastos: 12 meses reales de Abanca, sin los pagos
a Hacienda.

    scripts/casa-barbadas-plan.py                  # tabla de ayudas
    scripts/casa-barbadas-plan.py --itp 10         # si no sale el tipo reducido
    scripts/casa-barbadas-plan.py --aporte 918     # un caso concreto, detallado
"""
import argparse

PRECIO = 260_000.0
YA_PAGADO = 20_000.0
EN_MANO = 30_000.0
AHORRO_HOY = 26_300.0
HACIENDA_PENDIENTE = 4_921.36     # segundo plazo del IRPF, 5 de noviembre
NOMINA = 2_210.0
INGRESO_EXTRA = 350.0
GASTO_BASE = 1_104.0              # todo menos alquiler, con la cuota del coche dentro
ALQUILER_ACTUAL = 721.92
ALQUILER_COMPRA = 1_500.0
MESES_ALQUILER = 24
COCHE_CUOTA = 558.40
COCHE_PENDIENTE = 32_594.19
COCHE_TIN = 4.99 / 100 / 12
COMISION_CANCELACION = 0.01
GASTOS_FIJOS_FIRMA = 2_500.0      # notaría, registro, gestoría, tasación


def pendiente_coche(meses):
    b = COCHE_PENDIENTE
    for _ in range(meses):
        if b <= 0:
            return 0.0
        b -= COCHE_CUOTA - b * COCHE_TIN
    return max(b, 0.0)


def simular(espera, aporte, itp_pct, meses_alquiler=MESES_ALQUILER, detalle=False):
    """espera = meses hasta empezar el alquiler de compra. Devuelve (caja_minima, caja_final, hipoteca)."""
    caja = AHORRO_HOY - HACIENDA_PENDIENTE
    minimo = caja
    for mes in range(1, espera + 1):
        gasto = GASTO_BASE + ALQUILER_ACTUAL
        if pendiente_coche(mes - 1) <= 0:
            gasto -= COCHE_CUOTA
        caja += NOMINA + INGRESO_EXTRA + aporte - gasto
        minimo = min(minimo, caja)
    caja -= EN_MANO
    minimo = min(minimo, caja)
    if detalle:
        print(f"  mes {espera:>2}: entregas los {EN_MANO:,.0f} en mano, te quedan {caja:,.2f}")
    for mes in range(espera + 1, espera + meses_alquiler + 1):
        gasto = GASTO_BASE + ALQUILER_COMPRA
        if pendiente_coche(mes - 1) <= 0:
            gasto -= COCHE_CUOTA
        caja += NOMINA + INGRESO_EXTRA + aporte - gasto
        minimo = min(minimo, caja)
    fin = espera + meses_alquiler
    cancelacion = pendiente_coche(fin) * (1 + COMISION_CANCELACION)
    impuestos = PRECIO * itp_pct / 100 + GASTOS_FIJOS_FIRMA
    if detalle:
        print(f"  mes {fin:>2}: antes de la firma tienes {caja:,.2f}")
        print(f"          liquidar coche {cancelacion:,.2f} + impuestos y gastos {impuestos:,.2f}")
    caja -= cancelacion + impuestos
    minimo = min(minimo, caja)
    hipoteca = max(PRECIO - YA_PAGADO - EN_MANO - ALQUILER_COMPRA * meses_alquiler, 0)
    return minimo, caja, hipoteca


def espera_minima(aporte, itp_pct):
    for espera in range(0, 121):
        if simular(espera, aporte, itp_pct)[0] >= 0:
            return espera
    return None


def cuota_hipoteca(p, tin=3.0, anos=30):
    i = tin / 100 / 12
    n = anos * 12
    return p * i * (1 + i) ** n / ((1 + i) ** n - 1)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--itp", type=float, default=3.0)
    ap.add_argument("--aporte", type=float)
    ap.add_argument("--meses-alquiler", type=int, default=MESES_ALQUILER)
    args = ap.parse_args()

    impuestos = PRECIO * args.itp / 100 + GASTOS_FIJOS_FIRMA
    hipoteca = PRECIO - YA_PAGADO - EN_MANO - ALQUILER_COMPRA * args.meses_alquiler
    print(f"precio {PRECIO:,.0f} | ya pagado {YA_PAGADO:,.0f} | en mano {EN_MANO:,.0f} | "
          f"{args.meses_alquiler} meses x {ALQUILER_COMPRA:,.0f} que descuentan")
    print(f"hipoteca final {hipoteca:,.0f} -> cuota {cuota_hipoteca(hipoteca):,.0f}/mes al 3% a 30 años "
          f"({cuota_hipoteca(hipoteca)/NOMINA*100:.0f}% de tu nomina)")
    print(f"ITP {args.itp}% -> impuestos y gastos {impuestos:,.0f}")
    print(f"ahorro {AHORRO_HOY:,.0f} - {HACIENDA_PENDIENTE:,.0f} de Hacienda = {AHORRO_HOY-HACIENDA_PENDIENTE:,.0f}")
    print(f"tu solo: ahora {NOMINA+INGRESO_EXTRA-GASTO_BASE-ALQUILER_ACTUAL:+,.0f}/mes, "
          f"con el alquiler de compra {NOMINA+INGRESO_EXTRA-GASTO_BASE-ALQUILER_COMPRA:+,.0f}/mes\n")

    if args.aporte is not None:
        espera = espera_minima(args.aporte, args.itp)
        print(f"con {args.aporte:,.0f}/mes de tu pareja:")
        if espera is None:
            print("  no sale ni en 10 años")
            return
        _, caja, _ = simular(espera, args.aporte, args.itp, args.meses_alquiler, detalle=True)
        print(f"  empiezas el alquiler dentro de {espera} meses y firmas {args.meses_alquiler} despues "
              f"(mes {espera+args.meses_alquiler}), con {caja:,.2f} de margen")
        return

    print(f"{'ayuda de tu pareja':>28} {'empiezas en':>12} {'firmas en':>11}")
    casos = [
        ("nada", 0.0),
        ("media del alquiler actual (361)", ALQUILER_ACTUAL / 2),
        ("el alquiler actual entero (722)", ALQUILER_ACTUAL),
        ("solo el coche (558)", COCHE_CUOTA),
        ("coche + media alquiler (919)", COCHE_CUOTA + ALQUILER_ACTUAL / 2),
        ("coche + alquiler actual (1.280)", COCHE_CUOTA + ALQUILER_ACTUAL),
    ]
    for etiqueta, aporte in casos:
        espera = espera_minima(aporte, args.itp)
        if espera is None:
            print(f"{etiqueta:>28} {'nunca':>12} {'-':>11}")
        else:
            print(f"{etiqueta:>28} {'mes '+str(espera):>12} {'mes '+str(espera+args.meses_alquiler):>11}")


if __name__ == "__main__":
    main()
