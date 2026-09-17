#!/usr/bin/env python3
"""Plan de compra de la casa de Barbadás: cuánto tiene que aportar la pareja
cada mes y cuándo se puede entrar.

Modelo mensual desde hoy. El alquiler descuenta íntegro del precio, así que
entrar más tarde baja la hipoteca pero no cambia el dinero que hace falta en
efectivo. Lo que aprieta es el coche: el banco exige liquidarlo antes de dar
la hipoteca, y cancelarlo es un pago único.

    scripts/casa-barbadas-plan.py            # tabla de escenarios
    scripts/casa-barbadas-plan.py --itp 10   # si no sale el tipo reducido
"""
import argparse

PRECIO = 260_000.0
YA_PAGADO = 20_000.0        # 10.000 al dueño + 10.000 a la inmobiliaria
EN_MANO = 30_000.0          # sale de la remunerada al empezar el alquiler
AHORRO_HOY = 26_300.0
# Segundo plazo del IRPF de 2025, el 5 de noviembre. Sale de este mismo ahorro.
HACIENDA_PENDIENTE = 4_921.36
ALQUILER = 1_500.0          # descuenta del precio
# Base: 12 meses completos de Abanca (sep-2025 a ago-2026), la unica cuenta con
# historico largo. Lo gastado por Revolut aparece ahi como recargas, asi que
# cuenta una vez. Los pagos a Hacienda quedan fuera por extraordinarios.
NOMINA = 2_210.0            # 12 pagas, sin extras
INGRESO_EXTRA = 350.0       # bizums y devoluciones, media del año
GASTO_BASE = 1_104.0        # todo menos alquiler, CON la cuota del coche dentro
ALQUILER_ACTUAL = 721.92
COCHE_CUOTA = 558.40
COCHE_PENDIENTE = 32_594.19
COCHE_TIN = 4.99 / 100 / 12
COMISION_CANCELACION = 0.01  # habitual al cancelar anticipadamente
GASTOS_FIJOS_FIRMA = 2_500.0  # notaría, registro, gestoría, tasación


def pendiente_coche(meses):
    b = COCHE_PENDIENTE
    for _ in range(meses):
        b -= COCHE_CUOTA - b * COCHE_TIN
        if b <= 0:
            return 0.0
    return b


def simular(entrada_mes, aporte_pareja, itp_pct, en_mano=EN_MANO):
    """Caja acumulada al entrar, contando todo lo que entra y sale hasta ese mes."""
    meses_coche = 0
    for m in range(1, entrada_mes + 1):
        if pendiente_coche(m - 1) > 0:
            meses_coche += 1
    ingresos = (NOMINA + INGRESO_EXTRA + aporte_pareja) * entrada_mes
    # GASTO_BASE ya incluye la cuota del coche; al liquidarlo deja de pagarse
    gastos = (GASTO_BASE + ALQUILER) * entrada_mes - COCHE_CUOTA * (entrada_mes - meses_coche)
    cancelacion = pendiente_coche(entrada_mes) * (1 + COMISION_CANCELACION)
    impuestos = PRECIO * itp_pct / 100 + GASTOS_FIJOS_FIRMA
    caja = AHORRO_HOY - HACIENDA_PENDIENTE + ingresos - gastos - en_mano - cancelacion - impuestos
    hipoteca = max(PRECIO - YA_PAGADO - en_mano - ALQUILER * entrada_mes, 0)
    return caja, hipoteca, cancelacion, impuestos


def aporte_necesario(entrada_mes, itp_pct, en_mano=EN_MANO):
    caja, *_ = simular(entrada_mes, 0, itp_pct, en_mano)
    return max(-caja / entrada_mes, 0), caja


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--itp", type=float, default=3.0, help="tipo de ITP en %% (3 reducido, 10 general)")
    ap.add_argument("--en-mano", type=float, default=EN_MANO, help="pago en mano al empezar el alquiler")
    ap.add_argument("--aporte", type=float, help="aporte mensual de la pareja")
    args = ap.parse_args()

    impuestos = PRECIO * args.itp / 100 + GASTOS_FIJOS_FIRMA
    print(f"precio {PRECIO:,.0f} | ya pagado {YA_PAGADO:,.0f} | en mano {args.en_mano:,.0f} | alquiler {ALQUILER:,.0f}/mes que descuenta")
    print(f"ITP {args.itp}% -> impuestos y gastos de firma {impuestos:,.0f}")
    ahora = NOMINA + INGRESO_EXTRA - GASTO_BASE - ALQUILER_ACTUAL
    luego = NOMINA + INGRESO_EXTRA - GASTO_BASE - ALQUILER
    print(f"tu solo: ahora {ahora:+,.0f}/mes -> con alquiler de {ALQUILER:,.0f}: {luego:+,.0f}/mes\n")
    print(f"{'entrada':>9} {'hipoteca':>10} {'liquidar coche':>15} {'falta en total':>15} {'aporte pareja':>15}")
    for mes in (6, 12, 18, 24, 30, 36):
        need, caja = aporte_necesario(mes, args.itp, args.en_mano)
        _, hip, canc, _ = simular(mes, 0, args.itp, args.en_mano)
        print(f"{'mes '+str(mes):>9} {hip:>10,.0f} {canc:>15,.0f} {-caja:>15,.0f} {need:>12,.0f}/mes")
    if args.aporte is not None:
        print(f"\ncon {args.aporte:,.0f}/mes de tu pareja:")
        for mes in range(3, 61):
            caja, hip, _, _ = simular(mes, args.aporte, args.itp, args.en_mano)
            if caja >= 0:
                print(f"  entrada mas temprana: mes {mes} | hipoteca {hip:,.0f} | caja al entrar {caja:,.0f}")
                break
        else:
            print("  no sale en 60 meses")


if __name__ == "__main__":
    main()
