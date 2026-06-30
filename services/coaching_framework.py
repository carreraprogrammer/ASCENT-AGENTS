"""
services/coaching_framework.py

Base de conocimiento de coaching financiero destilada de la bibliografía.
Ver specs/research/metodologias-coaching-financiero.md en daniel15k-api para el detalle completo.

Uso: el agente consulta get_topic(topic) cuando necesita razonar profundo sobre una
situación específica. No es para uso operacional (registrar transacciones, consultar balance).
"""

from __future__ import annotations

FRAMEWORK: dict[str, dict] = {

    # ── Categorías por agencia ───────────────────────────────────────────────
    "categorias_agencia": {
        "resumen": (
            "El sistema clasifica el gasto por AGENCIA (cuánto margen de maniobra tiene el usuario "
            "sobre ese peso), no por tipo contable ni funcional. Son 3 tiers, no 6. La taxonomía es una "
            "síntesis de marcos de práctica (Conscious Spending Plan de Ramit Sethi + values-based "
            "budgeting) y se apoya en la literatura de control percibido / autoeficacia financiera "
            "(Cobb-Clark 2016; Asebedo 2019), que es lo que predice ahorro. OJO: la categorización por sí "
            "sola es un lever débil (RCT Clarity Money); lo que cambia conducta es la percepción de control "
            "+ la reflexión al clasificar. Por eso el agente PROPONE y el usuario confirma."
        ),
        "pregunta_unica": (
            "Toda categoría responde la MISMA pregunta — eso la hace estable: "
            "'Si la situación financiera empeora, ¿qué margen tengo sobre este gasto?'"
        ),
        "categorias": {
            "committed": (
                "Sin elección real. No puedo dejar de pagarlo sin incumplir una obligación legal/contractual. "
                "Arriendo, créditos (pago mínimo), seguros, servicios fijos. NO se baja con fuerza de voluntad."
            ),
            "necessary": (
                "Inevitable pero optimizable: aun en crisis el mínimo de esa función sigue > 0, aunque reduzca "
                "el monto. Mercado, transporte, salud, celular, herramientas de trabajo. Hay palancas de reducción."
            ),
            "flexible": (
                "Código DB: 'discretionary' (display 'Flexible'). Podría llevarse a CERO en una crisis. "
                "Restaurantes, delivery, ocio, ropa, cursos, suplementos, y el gasto relacional (subcat 'social'). "
                "Es el lugar con libertad real de corte y donde vive el coaching de mayor impacto. "
                "EXCEPCIÓN: un flexible puede estar marcado como 'prioridad defendida' (el usuario elige "
                "protegerlo por encima del orden por defecto) — reconocer la elección, no friccionar."
            ),
        },
        "arbol_decision": (
            "1) ¿Hay obligación contractual/legal? SÍ → committed. "
            "2) Si NO: ¿podría eliminarlo por completo en una crisis? SÍ → flexible (discretionary). "
            "NO (queda un mínimo > 0) → necessary."
        ),
        "fuera_del_eje": (
            "INVERSIÓN y SOCIAL ya no son tiers. El gasto 'en uno mismo' (cursos, suplementos, herramientas) "
            "cae en su tier por la pregunta única, no en una gaveta de 'inversión' (esa etiqueta invitaba a "
            "racionalizar — self-licensing). Lo social es la subcat 'social' bajo flexible. El AHORRO y la "
            "inversión-instrumento (fondo de emergencia, bolsillos, CDT, acciones) NO son gasto: van a metas/"
            "bolsillos (savings_goal / sinking_fund), nunca a una categoría de gasto."
        ),
        "diagnostico_clave": (
            "Si committed > 70% del ingreso base → el problema es ESTRUCTURAL, no de disciplina. "
            "Decirlo explícito: 'tu problema no es fuerza de voluntad, es que el 75% de tu ingreso ya está "
            "comprometido antes de que decidas nada'. Solución: cambios estructurales (renegociar, subir ingreso), "
            "no más restricción en flexible."
        ),
        "coaching_approach": (
            "Nombrar el tier al confirmar cada transacción refuerza conciencia sin moralizar. "
            "'$80K en delivery — fue flexible' es más poderoso que un regaño. "
            "El reconocimiento del tipo de gasto precede al cambio de conducta."
        ),
    },

    # ── Fondo de emergencia ──────────────────────────────────────────────────
    "fondo_emergencia": {
        "resumen": (
            "El fondo de emergencia es el único fundamento donde TODOS los frameworks coinciden: "
            "debe existir ANTES de atacar deuda agresivamente o invertir. "
            "Sin él, cualquier emergencia deshace meses de progreso y crea un ciclo difícil de romper."
        ),
        "el_ciclo_sin_fondo": (
            "Paga deuda agresivamente → llega emergencia (carro, médico, desempleo) → "
            "no tiene efectivo → emergencia va a la tarjeta → vuelve al mismo nivel de deuda → repite."
        ),
        "secuencia_correcta": [
            "Paso 1 — Starter fund: 1 mes de gastos bare-bones (TODOS los compromisos fijos "
            "incluyendo mínimos de deuda — en Colombia no pagarlos reporta a DataCrédito). "
            "Prioritario sobre deuda acelerada. Rompe el ciclo de deuda recurrente.",
            "Paso 2 — Atacar deuda agresivamente con todo el surplus.",
            "Paso 3 — Completar fondo a 3-6 meses bare-bones DESPUÉS de controlar la deuda. "
            "Con la deuda reducida, el cashflow liberado hace este paso mucho más rápido.",
        ],
        "regla_de_uso": {
            "descripcion": (
                "El fondo de emergencia SOLO se usa si los TRES criterios se cumplen simultáneamente. "
                "Si falta uno solo, no es emergencia."
            ),
            "criterios": {
                "inesperado": "No podías haberlo previsto ni ahorrado para ello con anticipación.",
                "necesario": "No actuar tiene consecuencias serias: perder el empleo, daño a la salud, "
                             "daño a propiedad esencial para trabajar o vivir.",
                "urgente": "No puede esperar al próximo ingreso. La urgencia es real, no emocional.",
            },
            "si_aplica": [
                "Pérdida de empleo o reducción severa de ingreso.",
                "Emergencia médica no cubierta por seguro.",
                "Falla de equipo esencial para generar ingreso (laptop, herramienta de trabajo).",
                "Daño estructural a la vivienda que la hace inhabitable.",
            ],
            "no_aplica": [
                "Oportunidades de compra (descuentos, gadgets, viajes).",
                "Gastos estacionales predecibles (SOAT, impuestos, navidad) → van a sinking funds.",
                "Reparaciones de mantenimiento esperadas → van a sinking funds.",
                "Cualquier cosa que puede esperar al próximo ingreso.",
                "Gastos sociales o de entretenimiento, por urgentes que se sientan.",
            ],
            "distincion_clave": (
                "Si podés PREVERLO → sinking fund. Si NO podés preverlo → fondo de emergencia. "
                "Son instrumentos distintos con propósitos distintos. "
                "Usar el fondo de emergencia para gastos previsibles destruye su función."
            ),
            "coaching_cuando_el_usuario_quiere_usarlo": (
                "Antes de validar el uso, hacer las tres preguntas en orden: "
                "1. '¿Podías haber sabido que esto iba a llegar?' "
                "2. '¿Qué pasa si no lo pagás hoy?' "
                "3. '¿Podés cubrirlo con el próximo ingreso sin consecuencias graves?' "
                "Si la respuesta a alguna lleva a 'no es emergencia', redirigir a otras opciones "
                "(cashflow del mes, sinking fund existente, o aceptar que es un gasto no planeado)."
            ),
        },
        "bare_bones": (
            "Bare-bones = TODAS las obligaciones recurrentes activas, incluyendo mínimos de deuda. "
            "En Colombia, no pagar mínimos de crédito reporta a DataCrédito y genera mora — "
            "son un costo de supervivencia tan real como el arriendo. "
            "El gasto discrecional se detiene en emergencia — no hay que financiarlo. "
            "Si gastos totales = $5M/mes pero bare-bones = $3.5M, el fondo objetivo es "
            "$3.5M × 3 = $10.5M, no $5M × 3 = $15M."
        ),
        "umbrales": {
            "0 meses": "Sin protección — máxima urgencia. Cualquier ingreso extra va al fondo.",
            "0.5-1 mes": "Starter fund — mínimo viable. Suficiente para proteger el plan de deuda.",
            "1-3 meses": "Zona de construcción — continuar llenando mientras se ataca deuda.",
            "3-6 meses": "Óptimo — recomendación CFP/Ramsey para empleo estable.",
            "6-9 meses": "Para ingreso variable o freelance.",
            ">9 meses": "Para dueños de negocio o alto riesgo laboral.",
        },
        "coaching_approach": (
            "No decir 'necesitás $11M de fondo de emergencia' como número suelto — eso paraliza. "
            "Decir: 'tu primera meta es $3.5M para llegar a 1 mes de colchón — con tu surplus actual "
            "podés lograrlo en X semanas'. Breaks it down to achievable steps."
        ),
        "prioridad_sobre_deuda": (
            "El argumento matemático dice que pagar deuda al 1.85% mensual (22% anual) primero es mejor "
            "que tener dinero al 0%. Pero el argumento conductual gana: sin cushion, el primer imprevisto "
            "destruye el plan y el usuario abandona. La consistencia supera la optimización puntual."
        ),
    },

    # ── Estrategia de deuda ──────────────────────────────────────────────────
    "deuda_estrategia": {
        "resumen": (
            "Dos estrategias principales. La elección no es solo matemática — "
            "el factor conductual determina cuál funciona para cada persona. "
            "El 60-70% de las personas que empiezan un plan de pago de deuda lo abandonan. "
            "La estrategia que el usuario puede sostener > la estrategia óptima que abandona."
        ),
        "snowball": {
            "descripcion": "Pagar mínimos en todo, todo el surplus a la deuda de MENOR saldo primero.",
            "ventaja": "Victorias rápidas → dopamina → momentum psicológico para continuar.",
            "desventaja": "Paga más interés total que avalanche (generalmente $1,000-3,000 más en deuda de $25M).",
            "cuando_usar": (
                "Usuario sin historial de consistencia, primer intento de pagar deuda, "
                "baja motivación, o cuando la diferencia de tasas entre deudas es pequeña (<3%)."
            ),
        },
        "avalanche": {
            "descripcion": "Pagar mínimos en todo, todo el surplus a la deuda de MAYOR tasa primero.",
            "ventaja": "Minimiza interés total pagado — matemáticamente óptimo.",
            "desventaja": "La primera victoria puede tardar meses — desmotiva a usuarios sin disciplina establecida.",
            "cuando_usar": (
                "Usuario analítico, historial de consistencia financiera, "
                "o cuando la diferencia de tasas entre deudas es grande (>5%)."
            ),
        },
        "regla_practica": (
            "Si la diferencia de tasa entre la deuda más cara y la más barata es < 3%, "
            "snowball y avalanche producen resultados similares — elegir por conducta, no por matemática. "
            "Si la diferencia > 5%, avalanche ahorra significativamente."
        ),
        "contexto_latam": (
            "En Colombia/LatAm: tasas de crédito de consumo (1.5-2%/mes = 18-24% anual) vs "
            "tarjetas de crédito (2-3%/mes = 24-36% anual). La TC generalmente es la primera "
            "en atacar bajo avalanche. CrediExpress y préstamos personales después."
        ),
        "coaching_approach": (
            "Preguntar primero: '¿en el pasado, cuando intentaste pagar deuda, qué te hizo parar?' "
            "Si la respuesta es frustración o falta de motivación → snowball. "
            "Si nunca lo intentó o tiene disciplina → ofrecer avalanche con la proyección de ahorro. "
            "Mostrar siempre la fecha estimada de payoff — hace el objetivo concreto y cercano."
        ),
    },

    # ── Ratios de salud financiera ───────────────────────────────────────────
    "ratios_salud": {
        "resumen": (
            "Los ratios de salud son métricas derivadas que diagnostican la situación estructural "
            "del usuario. Se calculan via GET /api/v1/health_metrics. "
            "No son juicios morales — son diagnósticos como un análisis de sangre."
        ),
        "ratio_fijos": {
            "formula": "recurring_obligations / base_budget_income",
            "rangos": {
                "≤50%": "Excelente — mucho margen de maniobra",
                "51-65%": "Saludable — dentro del rango objetivo",
                "66-75%": "Alerta — presión moderada, monitorear",
                ">75%": "Crítico — problema estructural, no de disciplina",
            },
            "implicacion_coaching": (
                "Ratio crítico → no recomendar 'gastar menos en ocio' como solución principal. "
                "El problema requiere intervención estructural: renegociar contratos, "
                "aumentar ingreso, o eliminar una obligación."
            ),
        },
        "dti": {
            "formula": "pagos_mensuales_deuda / base_budget_income",
            "rangos": {
                "≤20%": "Zona segura",
                "21-35%": "Advertencia — empezar estrategia de reducción",
                ">35%": "Estrés — deuda consume demasiado del ingreso base",
            },
            "nota": "Denominador es ingreso BASE (no variable) — mide estrés real de caja.",
        },
        "fondo_emergencia_meses": {
            "formula": "saldo_fondo_emergencia / bare_bones_monthly",
            "rangos": {
                "0 meses": "Sin protección — urgencia máxima",
                "0.5-1 mes": "Starter fund — mínimo viable",
                "1-3 meses": "En construcción",
                "3-6 meses": "Suficiente",
                ">6 meses": "Óptimo para ingreso variable",
            },
        },
        "tasa_ahorro": {
            "formula": "monthly_contribution_goals / base_budget_income",
            "rangos": {
                "<5%": "Insuficiente — casi sin construcción de futuro",
                "5-10%": "Básico — crecimiento lento",
                "10-15%": "Saludable — recomendación CFP mínima",
                ">15%": "Excelente — construcción real de patrimonio",
            },
        },
        "age_of_money": {
            "formula": "promedio(fecha_gasto - fecha_ingreso) para últimas 10 transacciones de egreso",
            "rangos": {
                "<14 días": "Paycheck-to-paycheck severo — gasta antes del próximo ingreso",
                "14-30 días": "Mejorando — un ingreso de buffer",
                "≥30 días": "Independiente del ciclo — gasta dinero del ciclo anterior",
            },
            "significado": (
                "YNAB Rule 4. Es la métrica más clara de si el usuario está rompiendo el ciclo "
                "paycheck-to-paycheck. No requiere que haga nada distinto — solo que el surplus "
                "se acumule hasta llegar a 30 días."
            ),
        },
    },

    # ── Fases financieras ────────────────────────────────────────────────────
    "fases_financieras": {
        "resumen": (
            "El financial_context.phase determina el foco de coaching. "
            "Cada fase tiene una prioridad de surplus distinta. "
            "La transición entre fases la decide el usuario — el agente sugiere cuando se cumplen condiciones."
        ),
        "fases": {
            "debt_payoff": {
                "descripcion": "Eliminar deuda de consumo como prioridad absoluta.",
                "surplus_destino": "100% a deuda (snowball o avalanche según estrategia).",
                "discretionary_limit": "15% del ingreso — restrictivo pero ejecutable.",
                "cuando_transicionar": (
                    "Cuando todas las deudas de consumo están liquidadas o DTI < 10%. "
                    "Sugerir transición a emergency_fund o investing."
                ),
                "coaching": (
                    "Hacer visible el progreso: 'X/Y deudas liquidadas, faltan ~Z meses a este ritmo'. "
                    "Celebrar cada deuda liquidada como hito real — el momentum es el activo más valioso."
                ),
            },
            "emergency_fund": {
                "descripcion": "Construir el fondo de emergencia completo (3-6 meses bare-bones).",
                "surplus_destino": "Todo al fondo hasta alcanzar el objetivo de meses.",
                "discretionary_limit": "20% del ingreso.",
                "cuando_transicionar": "Cuando meses_cobertura ≥ 3 (o 6 si ingreso variable).",
            },
            "investing": {
                "descripcion": "Construir activos con retorno. Tasa de ahorro objetivo ≥ 15%.",
                "surplus_destino": "Ahorro/inversión según vehículos disponibles.",
                "discretionary_limit": "25% del ingreso.",
                "coaching": (
                    "No recomendar instrumentos específicos (no es asesor de inversiones). "
                    "Sí calcular: 'a este ritmo de ahorro, en X años tendrías Y según la Regla del 25x'."
                ),
            },
            "wealth_building": {
                "descripcion": "Diversificación, real estate, negocios. Ingreso pasivo.",
                "discretionary_limit": "30% del ingreso.",
                "surplus_destino": "Mix entre inversión y metas personales explícitas.",
            },
        },
        "sin_fase_configurada": (
            "Si financial_context.phase es null: el agente no puede dar coaching estratégico preciso. "
            "Mencionar en el resumen: 'sin estrategia configurada el coaching es genérico'. "
            "Invitar a configurar con preguntas simples: '¿cuál es tu objetivo principal ahora — "
            "salir de deudas, construir un colchón, o empezar a invertir?'"
        ),
    },

    # ── Conducta financiera ──────────────────────────────────────────────────
    "conducta_financiera": {
        "resumen": (
            "Un meta-análisis de 201 estudios encontró que la educación financiera explica solo el 0.1% "
            "de la variación en comportamiento financiero. El problema no es conocimiento — es conducta. "
            "DALBAR Research: el fondo promedio retornó ~10% anual (1993-2013), el inversionista promedio "
            "ganó 3.7% — la diferencia es puramente conductual."
        ),
        "ciclo_vergüenza": {
            "descripcion": (
                "Dificultad financiera → vergüenza → evitación (deja de abrir estados de cuenta) → "
                "peores decisiones → mayor dificultad → más vergüenza."
            ),
            "vergüenza_vs_culpa": (
                "Culpa = 'hice algo malo' → motiva reparación. "
                "Vergüenza = 'soy malo/a' → motiva esconderse y paralizar. "
                "El coaching debe evitar inducir vergüenza a toda costa."
            ),
            "señales_de_alerta": (
                "El usuario desaparece de la app, deja de registrar transacciones, "
                "evita hablar de finanzas, o hace preguntas muy generales sin comprometerse. "
                "Respuesta: normalizar, no presionar."
            ),
        },
        "principios_mi": {
            "descripcion": "Motivational Interviewing — metodología basada en evidencia para cambio conductual.",
            "oars": {
                "O": "Open-ended questions: '¿Qué significaría para ti estar libre de deudas?'",
                "A": "Affirmations: 'Ya tomaste el paso difícil de registrar esto.'",
                "R": "Reflective listening: reflejar lo que dijo para demostrar que fue escuchado.",
                "S": "Summaries: integrar lo dicho para hacer visible el progreso.",
            },
            "change_talk": (
                "Reforzar cuando el usuario dice: 'quiero salir de deudas', 'sé que necesito hacer algo'. "
                "No amplificar cuando dice: 'pero es muy difícil', 'lo he intentado antes'."
            ),
        },
        "que_cambia_conducta": [
            "Identidad shift: 'soy alguien que ahorra' precede al ahorro.",
            "Victorias tempranas: dopamina del progreso visible sostiene el esfuerzo.",
            "Automatización: elimina la variable de fuerza de voluntad.",
            "Just-in-time: información relevante en el momento de decisión, no en tutoriales.",
            "Claridad del próximo paso: una sola acción concreta, no un plan completo.",
        ],
        "regla_de_oro": (
            "Si el usuario repite el mismo error mes a mes → el plan propuesto no es realista, "
            "no el usuario. Ajustar el plan antes de diagnosticar falta de disciplina."
        ),
        "tono_siempre": (
            "Normalizar antes de analizar. Nunca 'deberías haber', nunca comparar con un estándar externo. "
            "El pasado no es accionable — solo el futuro lo es. "
            "Contexto latinoamericano: el familismo (obligaciones con familia extendida) "
            "es una variable financiera real, no un 'gasto evitable'. Reconocerla sin juzgarla."
        ),
    },

    # ── Presupuesto discrecional ─────────────────────────────────────────────
    "presupuesto_discrecional": {
        "resumen": (
            "El límite discrecional es la palanca más visible del plan mensual. "
            "Calibrarlo demasiado bajo genera planes que se ignoran. "
            "El sistema usa ahora tasas dinámicas por fase — no 10% fijo."
        ),
        "tasas_por_fase": {
            "debt_payoff":    "15% del ingreso base — restrictivo, prioriza deuda",
            "emergency_fund": "20% del ingreso base",
            "investing":      "25% del ingreso base",
            "wealth_building": "30% del ingreso base",
            "sin_fase":       "15% del ingreso base (conservador por default)",
        },
        "marco_sethi": (
            "Sethi CSP: fixed 50-60%, investments 10%, savings 5-10%, guilt-free 20-35%. "
            "La idea: una vez que el sistema automático funciona, el gasto libre no requiere "
            "tracking — es el 'premio' por tener el sistema en orden."
        ),
        "anti_patron": (
            "Un límite discrecional de $648K/mes para alguien que históricamente gasta $1.6M "
            "en discrecional es un plan que nadie va a seguir. "
            "Mejor proponer $1M (realista, mejorable) que $648K (óptimo, ignorado). "
            "Regla: el plan ejecutable siempre gana al plan matemáticamente óptimo."
        ),
        "coaching_approach": (
            "Cuando el gasto discrecional real supera consistentemente el límite del plan, "
            "primero preguntar '¿cuánto creés que es razonable para vos?' antes de imponer un número. "
            "La autonomía en la decisión aumenta la probabilidad de cumplirla."
        ),
    },

    # ── Pagos anticipados ────────────────────────────────────────────────────
    "pagos_anticipados": {
        "resumen": (
            "Los pagos anticipados (arriendo del mes siguiente pagado antes de fin de mes, "
            "cuotas de crédito adelantadas) distorsionan el análisis del mes si no se identifican. "
            "El modelo de datos tiene covers_period_month/year para marcarlos explícitamente."
        ),
        "deteccion": [
            "La misma obligación recurrente aparece dos veces en el mes con montos iguales o similares.",
            "El concepto menciona explícitamente otro mes: 'Arriendo junio', 'cuota anticipada'.",
            "Un pago de committed en los últimos 3-5 días del mes que duplica un pago del inicio.",
            "La transacción tiene metadata.prepaid_obligation=true.",
        ],
        "ajuste_balance": (
            "balance_real_mes = balance_reportado + monto_pago_anticipado. "
            "saldo_efectivo_mes_siguiente = balance_actual + monto_anticipado (ya está cubierto). "
            "Siempre explicitar en el coaching: 'el balance incluye el [concepto] de [mes] "
            "pagado por adelantado. Sin ese pago, el mes cierra en $X'."
        ),
        "cuando_marcar": (
            "Al registrar la transacción, si el concepto o el contexto sugiere que cubre otro período, "
            "incluir covers_period_month y covers_period_year. "
            "El usuario también puede marcarlo desde la app."
        ),
        "regla_diagnóstico": (
            "Si el déficit del mes supera el 20% del ingreso mensual base, "
            "siempre revisar transacciones committed antes de reportar como crisis."
        ),
    },

    # ── Overflow y excedentes ────────────────────────────────────────────────
    "overflow_excedente": {
        "resumen": (
            "El overflow es el ingreso real por encima del base_budget_income del plan. "
            "No es dinero libre — su destino depende de la fase y del commitment_gap. "
            "La regla universal: ninguna recomendación puede superar el commitment_gap cuando es positivo."
        ),
        "reglas_por_fase": {
            "debt_payoff": (
                "100% del overflow a deuda. Primero pagar la deuda prioritaria según la estrategia "
                "(snowball: menor saldo, avalanche: mayor tasa). "
                "Solo si health_status == 'comfortable' y commitment_gap > 0."
            ),
            "emergency_fund": (
                "100% del overflow al fondo de emergencia hasta alcanzar el objetivo de meses."
            ),
            "investing": (
                "Split según la regla de overflow configurada en el plan. "
                "Si overflow_rule = 'mixed', usar overflow_rule_detail para los porcentajes."
            ),
        },
        "guardarrail_safe_to_deploy": (
            "safe_to_deploy = commitment_gap cuando es positivo. "
            "Nunca recomendar mover más de safe_to_deploy. "
            "Si health_status == 'critical': no recomendar mover nada — el usuario puede no llegar al próximo ingreso. "
            "Si health_status == 'warning': mencionar el margen estrecho antes de cualquier sugerencia."
        ),
        "error_comun": (
            "El ingreso extra NO es permiso para inflar el presupuesto base. "
            "Si Grupo 525 paga $3M extra en un mes, ese dinero tiene destino predefinido "
            "según la estrategia — no se convierte en más budget discrecional."
        ),
    },
}


def get_topic(topic: str) -> dict:
    """Retorna el framework para un tema específico."""
    if topic not in FRAMEWORK:
        return {
            "error": f"Tema '{topic}' no encontrado.",
            "temas_disponibles": list(FRAMEWORK.keys()),
        }
    return {"topic": topic, "framework": FRAMEWORK[topic]}


def available_topics() -> list[str]:
    return list(FRAMEWORK.keys())
