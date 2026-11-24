"""Funções-base para liquidação de verbas trabalhistas.

Use como módulo (from calc_trabalhista import *) ou rode `python calc_trabalhista.py`
para ver um exemplo. Todos os valores monetários usam Decimal; o arredondamento
(ROUND_HALF_UP, 2 casas) só acontece em `rs()`, no valor final de cada parcela.

Os parâmetros variáveis (salário mínimo, tabelas de INSS/IR, índices) NÃO ficam
fixos aqui: passe-os como argumento depois de confirmá-los em fonte oficial.
"""
from __future__ import annotations

import calendar
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal, ROUND_HALF_UP

D = Decimal

# Salário mínimo nacional por início de vigência (conferir antes de usar).
SALARIO_MINIMO = {
    date(2021, 1, 1): D("1100.00"),   # Lei 14.158/2021
    date(2022, 1, 1): D("1212.00"),   # Lei 14.358/2022
    date(2023, 1, 1): D("1302.00"),   # MP 1.143/2022
    date(2023, 5, 1): D("1320.00"),   # Lei 14.663/2023
    date(2024, 1, 1): D("1412.00"),   # Decreto 11.864/2023
    date(2025, 1, 1): D("1518.00"),   # Decreto 12.342/2024
    date(2026, 1, 1): D("1621.00"),   # Decreto 12.797/2025
}


def rs(valor: Decimal) -> Decimal:
    """Arredonda para centavos (ROUND_HALF_UP). Use só no valor final da parcela."""
    return D(valor).quantize(D("0.01"), rounding=ROUND_HALF_UP)


def salario_minimo_em(dia: date) -> Decimal:
    vigentes = [d for d in SALARIO_MINIMO if d <= dia]
    if not vigentes:
        raise ValueError(f"Salário mínimo de {dia} não cadastrado: informe o valor.")
    return SALARIO_MINIMO[max(vigentes)]


def add_meses(d: date, meses: int) -> date:
    m = d.month - 1 + meses
    ano, mes = d.year + m // 12, m % 12 + 1
    return date(ano, mes, min(d.day, calendar.monthrange(ano, mes)[1]))


# ---------------------------------------------------------------- aviso prévio
def anos_completos(admissao: date, referencia: date) -> int:
    anos = referencia.year - admissao.year
    if (referencia.month, referencia.day) < (admissao.month, admissao.day):
        anos -= 1
    return max(anos, 0)


def dias_aviso_previo(admissao: date, dispensa: date) -> int:
    """Lei 12.506/2011: 30 dias + 3 por ano completo, acréscimo máximo de 60 (total 90)."""
    return 30 + min(3 * anos_completos(admissao, dispensa), 60)


def projecao_aviso(dispensa: date, dias_aviso: int) -> date:
    """Data final projetada (art. 487, § 1º, CLT; OJ 82 e 83 da SDI-1).
    O dia da comunicação não conta: o aviso começa no dia seguinte."""
    return dispensa + timedelta(days=dias_aviso)


# ----------------------------------------------------------------------- avos
def dias_no_mes(inicio: date, fim: date, ano: int, mes: int) -> int:
    ini_mes = date(ano, mes, 1)
    fim_mes = date(ano, mes, calendar.monthrange(ano, mes)[1])
    a, b = max(inicio, ini_mes), min(fim, fim_mes)
    return (b - a).days + 1 if a <= b else 0


def avos_13(admissao: date, saida: date, ano: int) -> int:
    """13º proporcional: mês civil com 15 dias ou mais trabalhados = 1/12 (Lei 4.090/62, art. 1º, § 2º).
    `saida` deve ser a data já projetada pelo aviso indenizado, quando houver."""
    return sum(1 for m in range(1, 13) if dias_no_mes(admissao, saida, ano, m) >= 15)


def avos_ferias_proporcionais(inicio_periodo_aquisitivo: date, saida: date) -> int:
    """Férias proporcionais: 1/12 por mês do período aquisitivo ou fração superior a 14 dias
    (art. 146, parágrafo único, CLT). `saida` já projetada pelo aviso indenizado."""
    avos, ini = 0, inicio_periodo_aquisitivo
    while ini <= saida and avos < 12:
        fim_mes = add_meses(ini, 1) - timedelta(days=1)
        dias = (min(fim_mes, saida) - ini).days + 1
        if dias > 14:
            avos += 1
        ini = fim_mes + timedelta(days=1)
    return avos


def dias_ferias_por_faltas(faltas_injustificadas: int) -> int:
    """Art. 130 da CLT."""
    if faltas_injustificadas <= 5:
        return 30
    if faltas_injustificadas <= 14:
        return 24
    if faltas_injustificadas <= 23:
        return 18
    if faltas_injustificadas <= 32:
        return 12
    return 0


# ------------------------------------------------------------- jornada / horas
def divisor(jornada_semanal_horas: Decimal | int) -> Decimal:
    """Divisor = (jornada semanal ÷ 6) × 30 (art. 64 da CLT; Súm. 431; Tema 2 IRR do TST).
    44h → 220; 40h → 200; 36h → 180; 30h → 150.
    Bancário: 180 (6h) ou 220 (8h), sem alteração pelo sábado (Tema 2 IRR; Súm. 124)."""
    return (D(jornada_semanal_horas) / 6 * 30).quantize(D("0.0001"))


def valor_hora(base_salarial_mensal: Decimal, div: Decimal) -> Decimal:
    """Base = salário + parcelas salariais habituais (Súm. 264; Súm. 132, I; OJ 47; OJ 97)."""
    return D(base_salarial_mensal) / D(div)


def horas_extras(valor_h: Decimal, quantidade: Decimal, adicional: Decimal = D("0.5")) -> Decimal:
    return D(valor_h) * (1 + D(adicional)) * D(quantidade)


def horas_noturnas_fictas(horas_relogio: Decimal) -> Decimal:
    """Hora noturna reduzida de 52min30s (art. 73, § 1º): horas relógio × 60/52,5."""
    return D(horas_relogio) * D(60) / D("52.5")


def adicional_noturno(valor_h: Decimal, horas_fictas: Decimal, percentual: Decimal = D("0.2")) -> Decimal:
    return D(valor_h) * D(percentual) * D(horas_fictas)


def reflexo_rsr(valor_variavel_mes: Decimal, dias_uteis: int, domingos_feriados: int) -> Decimal:
    """RSR sobre parcela variável habitual (Súm. 172; Lei 605/49, art. 7º)."""
    return D(valor_variavel_mes) / D(dias_uteis) * D(domingos_feriados)


def rsr_repercute(competencia: date) -> bool:
    """OJ 394 (Tema 9 IRR): o RSR majorado pelas horas extras repercute em férias, 13º,
    aviso e FGTS somente para horas extras trabalhadas a partir de 20/03/2023.
    Para competências que atravessam a data, separe os dias antes e depois dela."""
    return competencia >= date(2023, 3, 20)


# --------------------------------------------------------------- adicionais
def insalubridade(grau: str, dia: date, base: Decimal | None = None) -> Decimal:
    """Art. 192 CLT. Base: salário mínimo (SV 4 + Rcl 6.266/STF), salvo base mais benéfica."""
    pct = {"minimo": D("0.10"), "medio": D("0.20"), "maximo": D("0.40")}[grau]
    return pct * (D(base) if base is not None else salario_minimo_em(dia))


def periculosidade(salario_base: Decimal) -> Decimal:
    """Art. 193, § 1º: 30% do salário-base (Súm. 191, I; ver item II para eletricitários antigos)."""
    return D("0.30") * D(salario_base)


# ------------------------------------------------------------------- FGTS
def fgts(base: Decimal, aliquota: Decimal = D("0.08")) -> Decimal:
    """8% (aprendiz 2%). Não incide sobre férias indenizadas (OJ 195)."""
    return D(base) * D(aliquota)


def multa_fgts(saldo_depositos_corrigidos: Decimal, percentual: Decimal = D("0.40")) -> Decimal:
    """40% sem justa causa / rescisão indireta; 20% culpa recíproca e art. 484-A.
    Base: soma dos depósitos devidos no contrato, corrigidos, incluídos os saques (OJ 42, I),
    SEM a projeção do aviso prévio indenizado (OJ 42, II; IRR 255)."""
    return D(saldo_depositos_corrigidos) * D(percentual)


@dataclass
class Linha:
    verba: str
    base: str
    criterio: str
    valor: Decimal
    natureza: str  # "salarial" ou "indenizatória"
    inss: bool
    fgts: bool


def imprimir_tabela(linhas: list[Linha]) -> Decimal:
    total = D(0)
    print(f"{'Verba':38} {'Natureza':14} {'INSS':5} {'FGTS':5} {'Valor (R$)':>14}")
    for l in linhas:
        v = rs(l.valor)
        total += v
        print(f"{l.verba:38} {l.natureza:14} {'sim' if l.inss else 'não':5} "
              f"{'sim' if l.fgts else 'não':5} {v:>14,.2f}")
    print(f"{'TOTAL':64} {total:>14,.2f}")
    return total


if __name__ == "__main__":
    # Exemplo: admissão 10/03/2021, dispensa sem justa causa em 05/11/2026, aviso indenizado.
    adm, dem = date(2021, 3, 10), date(2026, 11, 5)
    salario = D("3000.00")
    dias_av = dias_aviso_previo(adm, dem)
    saida_proj = projecao_aviso(dem, dias_av)
    print(f"Aviso: {dias_av} dias | saída projetada: {saida_proj:%d/%m/%Y}")
    av13 = avos_13(adm, saida_proj, saida_proj.year)
    ini_pa = date(2026, 3, 10)
    avfer = avos_ferias_proporcionais(ini_pa, saida_proj)
    print(f"Avos 13º {saida_proj.year}: {av13}/12 | Avos férias: {avfer}/12")
    linhas = [
        Linha("Saldo de salário (5 dias)", "salário/30", "3000/30×5", salario / 30 * 5, "salarial", True, True),
        Linha(f"Aviso prévio indenizado ({dias_av} dias)", "salário/30", "×dias", salario / 30 * dias_av, "indenizatória", False, True),
        Linha(f"13º proporcional ({av13}/12)", "salário", "/12×avos", salario / 12 * av13, "salarial", True, True),
        Linha(f"Férias proporcionais ({avfer}/12) + 1/3", "salário", "/12×avos×4/3", salario / 12 * avfer * D(4) / 3, "indenizatória", False, False),
    ]
    imprimir_tabela(linhas)
    print(f"Divisor 44h = {divisor(44)} | 40h = {divisor(40)} | 36h = {divisor(36)}")
    print(f"Insalubridade grau médio em 11/2026 = {rs(insalubridade('medio', date(2026, 11, 1)))}")
