# Daniel15K como sistema adaptativo de detección de vacíos y acompañamiento financiero

## Dirección estratégica

La mejora correcta no es construir un formulario largo disfrazado de chat, sino convertir Daniel15K en un sistema de **completitud contextual** que sabe qué le falta al usuario, detecta el momento adecuado para pedirlo y usa un tono de acompañamiento que preserve autonomía, reduzca carga cognitiva y evite vergüenza o resistencia. Esa dirección encaja con tres líneas de evidencia: la UX recomienda **progressive disclosure** y formularios con baja carga cognitiva; los sistemas adaptativos funcionan mejor cuando intervienen **justo a tiempo** y con soporte específico; y la literatura sobre entrevista motivacional y motivación autodeterminada muestra que el cambio sostenido mejora cuando la interacción es colaborativa, no coercitiva, y cuando el usuario siente autonomía, competencia y vínculo. citeturn24view0turn24view1turn29view0turn31view0turn17view0

También conviene cambiar la forma en que modelas la “conducta”. La evidencia reciente sugiere que “financial behavior” es un paraguas demasiado amplio y que, en realidad, conviene distinguir conductas diferentes —presupuestar, ahorrar, gastar, endeudarse y liquidar deudas— junto con determinantes psicológicos distintos como autoeficacia, actitudes, afecto, conocimientos y apoyo social. Además, un meta‑análisis en finanzas muestra que el autocontrol no debería tratarse como un rasgo moral fijo, sino como un conjunto de **estrategias accionables** que reducen gasto o aumentan ahorro con un efecto medio relevante. En otras palabras: clasificar a alguien como “impulsivo” o “controlado” es demasiado tosco para un sistema de coaching serio. citeturn35view0turn14view5turn35view1

Tu intuición de hacer de `monthly_plan` una entidad crítica es correcta. La investigación del bienestar financiero muestra que el éxito no es solo “tener datos”, sino poder cubrir obligaciones actuales, sentirse seguro respecto al futuro y mantener margen de elección. La guía de bienestar financiero y las herramientas de presupuesto para ingresos irregulares insisten en distinguir ingresos regulares e irregulares, construir flujo de caja y planear incluso cuando el dinero entra de manera variable. También hay evidencia de campo que vincula la planificación para gastos grandes e irregulares con una probabilidad mucho mayor de reportar salud financiera. citeturn14view6turn14view9turn27search0

Finalmente, si quieres que la app sea percibida como aliada, no basta con “ser amable”. Hay evidencia de que la vergüenza financiera puede empujar a la gente a retirarse y desentenderse de su situación; y, en sistemas conversacionales, la percepción de alianza, empatía, aceptación y colaboración está asociada a mejor vínculo y mejor experiencia. Eso implica que el wizard debe pedir contexto con una lógica explícita de ayuda, no de inspección. citeturn28search1turn32view0turn31view0

## Principios de diseño basados en evidencia

El primer principio es **preguntar poco, pero preguntar bien**. Progressive disclosure recomienda mostrar primero solo lo esencial y diferir opciones avanzadas o infrecuentes, porque eso mejora aprendizaje, eficiencia y tasa de error. En formularios, la carga cognitiva sube cuando el usuario tiene que interpretar demasiadas preguntas, recuperar demasiada información a la vez o cambiar de contexto constantemente; por eso, preguntar una o dos cosas por turno y agrupar preguntas relacionadas no es una preferencia estética, sino una decisión de usabilidad. citeturn24view0turn24view1

El segundo principio es **activar el wizard por necesidad contextual**, no por calendario fijo. La literatura sobre intervenciones adaptativas “just in time” define estos sistemas por tres rasgos: entregan soporte que responde a una necesidad real en tiempo real, adaptan contenido o timing según datos del sistema y se disparan automáticamente cuando hace sentido. Traducido a tu caso: no preguntes “porque toca”, sino cuando falte un dato que bloquea una acción importante o cuando el usuario se encuentre en un momento naturalmente relevante, como pedir un presupuesto, analizar deudas o revisar el mes. citeturn29view0

El tercer principio es **usar un estilo de acompañamiento compatible con autonomía**. La entrevista motivacional se describe como un estilo centrado en la persona, colaborativo, con escucha reflexiva, toma de decisiones compartida y lenguaje no confrontacional. La investigación que conecta ese enfoque con la teoría de autodeterminación subraya que el cambio se sostiene mejor cuando la persona percibe elección, sentido y capacidad, no presión. En finanzas personales, la motivación autónoma se asocia con ahorro, inversión, autoeficacia y bienestar financiero; la motivación controlada se asocia negativamente con bienestar, y la amotivación se asocia con sobregasto. citeturn31view0turn17view0

El cuarto principio es **modelar mecanismos, no defectos personales**. En vez de guardar “usuario impulsivo”, te conviene guardar evidencia sobre: qué conductas realiza, qué barreras tiene, qué señales de estrés aparecen, qué tanta autoeficacia reporta, qué tan dependiente es del crédito, y qué estrategias ya le funcionan. Esto encaja mejor con marcos como COM‑B, que leen la conducta como resultado de capacidad, oportunidad y motivación, y con la revisión reciente que pide distinguir conductas financieras específicas y sus determinantes concretos. citeturn14view1turn35view0turn35view1

## Estado de completitud y motor de gaps

La mejor base técnica para Daniel15K es un **completeness graph** por usuario. Ese grafo no debería limitarse a “respondió / no respondió”, sino distinguir al menos seis estados por campo o dimensión: `missing`, `partial`, `sufficient`, `confirmed`, `stale` y `conflicting`. A cada nodo le conviene agregar `confidence`, `source`, `last_confirmed_at`, `freshness_policy` y `derived_from`, porque un sistema adaptativo necesita saber no solo qué sabe, sino **qué tan confiable y vigente** es lo que sabe. Esa granularidad es coherente con la recomendación de describir intervenciones y sus ingredientes con precisión, en vez de usar categorías demasiado gruesas. citeturn30view0turn14view6

Una forma práctica de modelarlo es la siguiente:

| Dimensión | Mínimo para `sufficient` | Bloquea funciones críticas | Frescura sugerida |
|---|---|---|---|
| `income_profile` | al menos una fuente base neta, cadencia, confiabilidad, y distinción base/variable | sí | 90 días |
| `debts` | lista de deudas activas, saldo, pago mínimo o cuota, y prioridad elegida o inferida | sí para deuda; parcial para presupuesto | 30 días |
| `recurring_expenses` | obligaciones fijas relevantes, monto, periodicidad y próximo vencimiento estimado | sí | 60 días |
| `strategy` | objetivo principal, preferencia de reducción de deuda, regla sobre excedentes | sí | 180 días |
| `monthly_plan` | `base_budget_income`, `mode`, `discretionary_limit`, `overflow_rule`, confirmación del mes | sí, y es el principal “switch” operativo | mensual |
| `behavior_profile` | suficientes señales para personalizar feedback, no para bloquear | no | 90 días |

Tu función `detect_missing_context(user_id)` debería recibir también el **intent** actual y devolver no solo dimensiones faltantes, sino una cola priorizada con justificación. En términos de producto, el orden base que propones es razonable, pero debería modularse por intención. Si el usuario quiere “salir de deudas”, `debts` y `strategy` suben. Si quiere “hacer presupuesto”, `income_profile`, `recurring_expenses` y `monthly_plan` dominan. Si quiere solo clasificar movimientos, el sistema puede seguir con un nivel parcial y activar un **soft prompt** en lugar de un bloqueo. Esa lógica de priorización por necesidad inmediata y carga de usuario está alineada con los principios de soporte adaptativo y reducción de fricción. citeturn29view0turn24view1

Una implementación útil sería separar tres tipos de gap. El primero es **informational gap**, cuando falta un dato objetivo. El segundo es **policy gap**, cuando el sistema tiene datos, pero no reglas de decisión; por ejemplo, sabe ingresos y gastos, pero no sabe qué hacer con el excedente. El tercero es **behavioral gap**, cuando el sistema puede operar, pero no personalizar bien la intervención porque no entiende barreras, motivación o señales de evitación. Este tercer tipo es especialmente importante si tu objetivo es “guiar conducta, no solo registrar datos”. citeturn14view1turn17view0turn35view0

Una firma útil sería esta:

```ts
type MissingDimension = {
  dimension:
    | "income_profile"
    | "debts"
    | "recurring_expenses"
    | "strategy"
    | "monthly_plan"
    | "behavior_profile";
  status: "missing" | "partial" | "stale" | "conflicting";
  priority: number;
  blocking: boolean;
  why_now: string;
  next_question_key: string;
};

detect_missing_context(userId: string, intent?: UserIntent): MissingDimension[]
```

Y el priorizador puede usar una fórmula de este estilo:

```ts
priority =
  criticality_weight
  + intent_relevance_weight
  + missingness_weight
  + staleness_weight
  + contradiction_weight
  + distress_modifier
  - recent_prompt_fatigue_penalty
```

La clave no es la matemática exacta, sino el principio: **preguntar lo mínimo que destraba la mejor acción siguiente**.

## Wizard adaptativo y generación del plan mensual

El wizard debería comportarse como un **micro‑coach por turnos**, no como una encuesta. El flujo recomendado es: explicar brevemente por qué pregunta, pedir una sola pieza de información principal, hacer como máximo un follow‑up conectado, resumir lo entendido, persistir la respuesta, recalcular completitud y decidir si sigue preguntando o si ya puede actuar. Ese patrón coincide con la evidencia sobre progressión por etapas, baja carga cognitiva, apoyo adaptativo y conversación centrada en la persona. citeturn24view0turn24view1turn29view0turn31view0

Para `income_profile`, la mejor secuencia no es “enumera todos tus ingresos”, sino empezar por el **ancla más estable**: “¿Cuál es el ingreso que con más seguridad entra todos los meses?” Después recién preguntar por comisiones, propinas, ventas o ingresos adicionales. Las herramientas para ingresos irregulares recomiendan distinguir entre ingresos regulares, irregulares, estacionales y de una sola vez, precisamente para prepararse para meses con menos entrada. Esa separación es la base para tu `base_budget_income`. citeturn14view9

Para `debts`, el wizard debería captar lo necesario para decidir, no para hacer arqueología contable. El set mínimo es: acreedor o tipo, saldo, pago mínimo, tasa si existe y qué deuda le “duele” más al usuario. Tu sistema puede ofrecer dos estrategias: **avalanche** si la prioridad es minimizar interés, y **snowball** si la prioridad es ganar impulso con victorias tempranas. La guía pública de reducción de deuda describe ambas: la primera ahorra dinero a largo plazo; la segunda da sensación rápida de progreso. Un estudio de comportamiento observó además que cerrar cuentas pequeñas puede ayudar a sostener el esfuerzo de salida de deuda. citeturn14view8turn14view7

Para `recurring_expenses`, busca primero la estructura fija que determina la supervivencia del mes: arriendo, servicios, colegio, transporte, suscripciones duras, pagos de salud y mínimos inevitables. Aquí conviene preguntar por **monto mensual** y, si aplica, por fecha aproximada de pago, porque el flujo de caja importa especialmente cuando el ingreso es irregular. La literatura práctica sobre ingresos variables insiste en mapear cuándo entra el dinero y cuándo caen los gastos, no solo cuánto suman al final del mes. citeturn14view9turn27search5

Para `strategy`, yo no guardaría solo `goal = debt|stability|growth`. Guardaría también el **criterio de trade‑off** del usuario. Por ejemplo: “si entra plata extra, ¿prefieres bajar deuda, construir colchón, o invertir una parte?” y “¿te motiva más ahorrar intereses o ver avances rápidos?”. Esto te da una estrategia ejecutable y además respeta autonomía, porque el sistema guía sobre consecuencias en vez de imponer una doctrina única. citeturn31view0turn14view8turn14view7

La generación de `monthly_financial_plan` debería ocurrir cuando el sistema tenga un mínimo suficiente de ingresos, gastos estructurales, deudas y estrategia. Mi recomendación es que el plan se calcule en dos capas. La primera capa es el **piso confiable**: el ingreso base con el que el usuario puede comprometerse sin depender de extras. La segunda capa es la **política de variabilidad**: qué hacer cuando entren ingresos por encima del piso. En usuarios con ingresos variables, este diseño es más robusto que presupuestar sobre el promedio ingenuo, porque las herramientas de presupuesto para ingreso irregular están diseñadas precisamente para que la persona sobreviva cuando entra menos dinero del esperado. citeturn14view9turn27search5

Yo implementaría dos modos. En `conservative`, `base_budget_income` se calcula a partir del ingreso recurrente más confiable y descuenta lo no verificable o muy volátil. En `expected`, puedes permitir una fracción prudente del componente variable, siempre separándolo del piso. La razón de fondo es que distinguir lo regular de lo irregular prepara mejor para meses flojos y ayuda a construir flujo de caja realista; además, planear por adelantado para gastos irregulares se asocia fuertemente con mejor salud financiera. citeturn14view9turn27search0

Una estructura recomendable sería:

```ts
type MonthlyFinancialPlan = {
  user_id: string;
  month: string; // 2026-04
  status: "draft" | "confirmed" | "superseded";
  mode: "conservative" | "expected";
  base_budget_income: number;
  expected_variable_income?: number;
  recurring_obligations_total: number;
  debt_minimums_total: number;
  protected_buffer_amount: number;
  discretionary_limit: number;
  overflow_rule: "debt" | "emergency_fund" | "investment" | "mixed";
  overflow_rule_detail?: Record<string, number>;
  reward_pct?: number;
  investment_target?: number;
  debt_strategy?: "snowball" | "avalanche" | "custom";
  assumptions: Record<string, unknown>;
  confirmed_at?: string;
}
```

Una mejora especialmente valiosa es convertir las reglas del plan en **implementation intentions**. En vez de guardar solo “overflow_rule = debt”, guarda reglas tipo: “si entra ingreso extra, entonces primero envío 100% a la deuda prioritaria” o “si el gasto discrecional supera el 80% antes del día 20, entonces congelo gastos de ocio el resto del mes”. La literatura sobre if‑then planning la describe como una forma de cerrar la brecha entre intención y acción, especificando cuándo, dónde o bajo qué señal ocurre la respuesta. citeturn20search3turn20search9

La confirmación con el usuario debe sonar así: resumida, concreta y con elección. No “este es tu plan”. Mejor: “Con lo que me contaste, este mes te propongo vivir sobre 6.4M, dejar 600k como tope discrecional y mandar cualquier extra a deuda. ¿Lo confirmamos así o quieres ajustar algo?” Ese patrón preserva autonomía, explicita racionalidad y empata con el estilo de acompañamiento que la entrevista motivacional considera más favorable al cambio sostenido. citeturn31view0turn17view0

## Modelo de conducta y tono aliado

Tu intuición de guardar conducta “con base en evidencia científica” es muy buena, pero conviene hacerlo de forma **mecanicista y editable**, no patologizante. Mi recomendación es un `behavior_profile_v2` que no clasifique personas como buenas o malas administradoras, sino que registre **qué barreras cambian el comportamiento y qué intervenciones sí funcionan**. La revisión reciente de money‑management sugiere separar conductas específicas y correlatos específicos; el estudio de motivación en finanzas sugiere distinguir motivación autónoma, controlada y amotivación; y la literatura de autocontrol financiero sugiere pensar en estrategias antes que en “fuerza de voluntad”. citeturn35view0turn17view0turn35view1

Una versión útil del modelo tendría seis ejes. `money_management_domains`: cómo se comporta en presupuesto, ahorro, gasto, crédito y pago de deudas. `motivation_quality`: qué tanto el usuario actúa por convicción propia versus presión, culpa o resignación. `self_efficacy_and_control`: cuánto siente que puede ejecutar el plan. `monitoring_habit`: si revisa, anota o ignora. `credit_reliance`: si la tarjeta funciona como medio de pago o como financiamiento recurrente. `stress_and_shame_risk`: si aparecen evitación, culpa, postergación o colapso ante conversaciones financieras. Ese perfil es mucho más útil para personalizar que una sola etiqueta binaria. citeturn35view0turn17view0

Además, yo separaría **perfil** de **evidencia**. Por ejemplo: si el usuario dice “siempre me desordeno cuando cobro”, eso alimenta `risk_windows = payday`. Si las transacciones muestran sobregasto en fines de semana, eso alimenta `spending_context = weekend_social`. Si ya aceptó una regla tipo “si gasto 80% del ocio antes del día 15, freno”, eso alimenta `successful_strategies`. La razón práctica es que la taxonomía de técnicas de cambio de conducta trata estas piezas como ingredientes observables y replicables —prompts, monitoreo, feedback, action planning, goal setting— y esa precisión mejora diseño, réplica y medición. citeturn30view0turn29view0

Para que la app se sienta como aliada, el copy debe cumplir tres reglas. Primero, **normalizar sin trivializar**: “muchas personas con ingresos variables sienten que el presupuesto no les sirve; por eso vamos a armar uno que sí aguante meses flojos.” Segundo, **pedir permiso y ofrecer elección**: “¿te hago dos preguntas para dejarte un plan más seguro?” Tercero, **reflejar y afirmar**: “ya tienes claro que tu problema no es cuánto ganas, sino que los extras se te diluyen; eso es una señal muy útil para armar la regla correcta.” Este estilo encaja con empatía, aceptación, colaboración y apoyo a la autonomía. citeturn31view0turn33view1

La razón para ser tan cuidadoso con el lenguaje no es cosmética. La investigación sobre vergüenza financiera sugiere que la vergüenza puede empeorar la dificultad económica porque empuja a retirarse y desentenderse. En paralelo, los estudios de agentes conversacionales muestran que aceptación, seguridad, cuidado y no juicio favorecen vínculo y disposición a continuar. Si el usuario siente que Daniel15K lo regaña, esconderá información, pateará decisiones o abandonará el flujo. citeturn28search1turn32view0

## Persistencia e integración con agentes, API, UI y mensajería

Tu mapeo general de persistencia es bueno, pero conviene volverlo más explícito y operativo. `income_profile` debería persistirse principalmente en `income_sources`, añadiendo campos como `classification` (`base`, `variable`, `seasonal`, `one_time`), `net_amount`, `cadence`, `reliability_score`, `last_confirmed_at` y `evidence_source`. `debts` puede vivir en `debts`, pero necesita `minimum_payment`, `apr`, `priority_rank`, `strategy_eligible` y `last_confirmed_at`. `recurring_expenses` puede ir a `recurring_obligations` con `amount`, `cadence`, `essentiality`, `next_due_estimate` y `is_structural`. `strategy` cabe bien en `financial_context`, pero yo le agregaría `primary_goal`, `debt_strategy`, `overflow_preference`, `reward_pct`, `coaching_preferences` y `motivation_notes`. `behavior_profile` puede empezar como entidad separada o como JSONB versionado, pero idealmente merece vida propia. Y `monthly_plan` sí debería ser una entidad nueva, versionada por mes, porque es el “contrato operativo” que usarán presupuesto, interpretación de transacciones y feedback. citeturn14view6turn30view0

A nivel de backend, añadiría dos piezas nuevas. La primera es `wizard_sessions`, que guarda en qué paso está el usuario, qué ya se le preguntó, cuál fue la última respuesta parseada, y cuál es la siguiente mejor pregunta. La segunda es `completeness_state`, que puede ser una tabla materializada o una proyección cacheada con el estado de cada dimensión y su prioridad. Si quieres escalar bien, también conviene un `question_event_log` y un `plan_generation_event_log` para análisis posterior y experimentación. citeturn29view0turn30view0

La integración con el agente debería ocurrir **antes** de la respuesta normal. El flujo ideal sería: el agente identifica intención, llama al servicio de completitud, revisa si hay `blocking gaps`, y decide entre tres rutas. Ruta normal, si no faltan datos críticos. Ruta de wizard bloqueante, si la intención requiere contexto que no existe. Ruta de soft nudge, si faltan datos importantes pero no bloqueantes. Cuando el usuario contesta, el sistema parsea, persiste, recalcula completitud y continúa exactamente donde quedó, no desde cero. Ese patrón es compatible con la lógica de JITAI y evita que el usuario sienta que abandonó su objetivo original. citeturn29view0turn31view0

En API, esto se puede implementar con piezas relativamente limpias:

```ts
GET  /users/:id/completeness
POST /users/:id/context/detect-missing
POST /wizard/:sessionId/answer
POST /plans/monthly/generate
POST /plans/monthly/:planId/confirm
POST /agents/:userId/preflight
```

`preflight` sería el “guardian” del agente. Devuelve algo como:

```json
{
  "mode": "wizard",
  "reason": "missing_critical_context",
  "missing": ["income_profile", "monthly_plan"],
  "next_prompt": {
    "dimension": "income_profile",
    "question_key": "income_base_primary"
  }
}
```

Para el canal conversacional, la mejor arquitectura es reutilizar el mismo `wizard service` tanto en UI como en mensajería. En Telegram, el Bot API es HTTP‑based, admite webhooks para recibir actualizaciones de forma push y ofrece dos mecanismos útiles para este caso: **reply keyboards** para respuestas rápidas predefinidas y **inline keyboards** para navegación o cambios de estado sin ensuciar el chat con más mensajes. Además, las Mini Apps permiten abrir una interfaz más rica dentro del propio canal cuando haga falta revisar o editar el plan mensual completo. En la práctica, eso te deja hacer conversaciones ligeras por mensaje y saltar a una vista más estructurada solo cuando la tarea lo amerita. citeturn34view0turn34view2turn34view1turn34view3

## Ejemplos operativos y hoja de ruta

### Conversaciones de ejemplo

**Caso nuevo**

Sistema: “Para ayudarte bien con tu mes, primero necesito entender con qué ingreso sí puedes contar. No te voy a pedir todo de una. Empecemos por lo principal: ¿cuál es el ingreso que con más seguridad te entra cada mes?”  
Usuario: “Mi salario, 6.400.000.”  
Sistema: “Perfecto. Ese será tu piso confiable. ¿Además de eso te entra algo variable, como comisiones, ventas o trabajos extra?”  
Usuario: “Sí, a veces comisiones, pero no siempre.”  
Sistema: “Listo. Entonces por ahora voy a tratar las comisiones como ingreso extra, no como base. Ahora necesito tus gastos fijos más importantes para armar un plan que aguante incluso un mes flojo. ¿Cuánto pagas de arriendo?”  

**Caso parcial**

Sistema: “Puedo ayudarte con el presupuesto, pero me falta una sola pieza crítica: tu ingreso base confiable. La necesito para no proponerte un plan que después te asfixie. ¿Con cuánto sí puedes contar este mes, sin incluir extras?”  
Usuario: “4.200.000.”  
Sistema: “Perfecto. Con eso ya destrabamos el presupuesto. Sigo con una sola pregunta más: ¿quieres que cualquier ingreso extra vaya primero a deuda o a ahorro?”  

**Caso de bloqueo al intentar presupuestar**

Usuario: “Hazme el presupuesto del mes.”  
Sistema: “Lo hago encantado, pero me falta el dato que define todo el plan: tu base mensual. Prefiero pedirte esto ahora y no inventar números. ¿Cuál es el neto con el que sí puedes contar?”  

**Caso completo**

Usuario: “¿Cómo voy este mes?”  
Sistema: “Vas bien: estás dentro del tope discrecional y cualquier ingreso extra sigue yendo a deuda, tal como confirmaste para este mes. No necesito hacerte más preguntas por ahora.”

### Cambios mínimos viables

El MVP razonable no necesita un rediseño total. Necesita cinco piezas. Una proyección de `completeness_state`. Un `wizard_session` simple con 1–2 preguntas por turno. La nueva entidad `monthly_financial_plans`. Un `agent preflight` que decida si responde normal o entra al wizard. Y microcopy bien escrito para unas 20–30 preguntas de alta calidad. Con eso ya cubres tus cuatro casos de éxito: nuevo usuario, usuario parcial, intento bloqueado por falta de base, y usuario completo al que no se le molesta. citeturn24view0turn29view0turn31view0

### Camino ideal

La versión ideal agrega inferencia y personalización real. Por ejemplo: sugerir montos de arriendo o servicios a partir de transacciones recurrentes, marcar conflictos cuando el usuario dice “no tengo deudas” pero hay pagos rotativos de tarjeta, detectar estacionalidad de ingresos, recalcular staleness automáticamente al cambiar de mes, y adaptar el estilo de coaching según señales de motivación, autoeficacia y evitación. También ahí tiene mucho valor guardar qué **estrategias de autocontrol** ayudaron realmente a cada usuario, porque la evidencia sugiere que no hay una única táctica ideal para todos, sino un repertorio de estrategias con eficacia agregada relevante. citeturn35view1turn35view0

### Métricas de éxito

Tus métricas deberían mezclar completitud, comportamiento y experiencia. En completitud: porcentaje de usuarios con `income_profile`, `strategy` y `monthly_plan` confirmados; tiempo al primer plan confirmado; y abandono por pregunta del wizard. En comportamiento: adherencia al `discretionary_limit`, reducción de mora, avance en deuda prioritaria, uso de overflow rule y estabilidad de fin de mes. En experiencia: tasa de aceptación del wizard, número de turnos por cierre de gap, percepción de apoyo, percepción de juicio o fricción, y eventualmente un pulso periódico de bienestar financiero. Usar una medida estandarizada de bienestar financiero te da una forma más seria de verificar si el sistema mejora seguridad presente, seguridad futura y libertad de elección, en vez de medir solo “respuestas completadas”. citeturn14view6turn32view0

La recomendación final es clara: Daniel15K debería evolucionar de “motor que responde” a **motor que detecta incompletitud, pide el mínimo dato de mayor impacto, construye un plan mensual confirmable y luego usa ese plan para intervenir sobre comportamiento en momentos relevantes**. Si haces esa transición con completitud explícita, reglas de disparo sensibles al contexto, una psicología más fina que el binario impulsivo/controlado y un tono de autonomía‑soporte en vez de juicio, no solo recogerás mejor data: construirás un sistema que el usuario puede sentir como aliado. citeturn29view0turn31view0turn17view0turn28search1turn35view0turn35view1