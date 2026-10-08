# Agendador de Revisões (Excel)

Planilha que monta um calendário de estudos com **revisões espaçadas automáticas**,
nas visões **Mês / Semana / Tabela + Painel**, no estilo do calendário do app de mentoria.

| Arquivo | O que é |
|---|---|
| `Agendador_de_Revisoes.xlsx` | Já preenchido com as aulas teóricas do print (27/09 a 27/10/2026) como exemplo |
| `Agendador_de_Revisoes_em_branco.xlsx` | Mesma planilha, vazia, para começar do zero |
| `gerar_agendador_revisoes.py` | Gerador (openpyxl) — regenera os dois arquivos |

## Como usar

1. **Temas** — digite Tema, Grande Área e Data da aula (células amarelas). A aula e as
   revisões R1…R5 (padrão: +1, +7, +15, +30 e +60 dias) são calculadas sozinhas.
2. **Avulsos** — simulados, blocos de questões e compromissos com data fixa.
3. **Calendário** — escolha Mês e Ano no topo; cada dia mostra até 6 itens coloridos por
   categoria (Teórica, Revisão, Prática, Simulado, Outro) e "+N mais" quando passa disso.
4. Marque **✓** (ou `x`) na coluna *Feito* da etapa: o item fica riscado no calendário.

Intervalos, categorias de cada etapa, dia de descanso e áreas ficam na aba **Config**.

## Regenerar

```bash
python gerar_agendador_revisoes.py
```

O gerador grava só as fórmulas; ao abrir no Excel elas são calculadas. Para gravar os
valores já calculados (pré-visualizações), recalcule com o LibreOffice.
