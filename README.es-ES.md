

# `textual-debugger`

El paquete `textual-debugger` proporciona `tdb` (la herramienta de línea de comandos y el módulo), un depurador completo basado en terminal para Python y otros lenguajes con una implementación del Protocolo de Adaptador de Depuración (DAP). El soporte para C y C++ (vía `gdb` o `lldb-dap`) está integrado.

`tdb` está construido con [textual](https://github.com/Textualize/textual) y se comunica mediante DAP con un adaptador de depuración enlazable: [debugpy](https://github.com/microsoft/debugpy) (el motor detrás del depurador de Python de VS Code) para Python, el [modo DAP de GDB](https://sourceware.org/gdb/current/onlinedocs/gdb.html/Debugger-Adapter-Protocol.html) o [lldb-dap](https://lldb.llvm.org/resources/lldbdap.html) para código compilado. Proporciona una rica interfaz interactiva para ejecutar paso a paso, inspeccionar variables, gestionar puntos de interrupción y evaluar expresiones en programas complejos.

- PyPI: https://pypi.org/project/textual-debugger/
- GitHub: https://github.com/AlDanial/tdb

Licencia MIT. Copyright 2026 por Al Danial.

## Resumen de Características

`tdb`:

- Depura múltiples lenguajes a través del Debug Adapter Protocol: Python (vía `debugpy`, la colección más rica de características) y C/C++ (vía `gdb -i dap` o `lldb-dap`), con detección automática del lenguaje según el objetivo (ref. [Depuración Multi-Lenguaje](#multi-language-debugging)).

- Soporta la depuración de código Python síncrono, asíncrono, multihilo y multiproceso. Soporta específicamente los módulos
    - `asyncio` (con un inspector de tareas asíncronas integrado y un gráfico de espera de tareas)
    - `threading` (con un inspector de hilos)
    - `multiprocessing` / `concurrent.futures` (con adjuntamiento automático de procesos hijos y un inspector de procesos)

- Soporta el adjuntamiento remoto a programas Python habilitados para debugpy

- Incluye un modo servidor JSON-RPC, un modo MCP y un archivo `SKILL.md` que habilitan el control programático de depuración, haciéndolo adecuado para flujos de trabajo de depuración automatizada y sin interfaz gráfica, así como para depuración asistida por IA

- Puede iniciar el programa depurado en una terminal externa para permitir depurar aplicaciones TUI construidas con `textual`, `prompt-toolkit`, `urwid`, `curses`, `rich`, y similares

- Viene con un gancho de excepción post-mortem que puede instalarse en programas Python para que `tdb` se abra automáticamente en la primera excepción no controlada

- Puede operarse completamente desde el teclado, lo que lo hace adecuado para entornos no gráficos (el soporte de mouse está disponible en entornos gráficos)

## Agradecimientos

Gracias a:

- Will McGugan por el increíble módulo `textual`.
`tdb` sería una pálida sombra de sí mismo si hubiera usado cualquier otro marco TUI.
Trabajo fantástico, Will.

- Microsoft por el Debug Adapter Protocol (DAP) y por liberar
su implementación en `debugpy` y la extensión Python Debugger para Visual Studio Code
como código abierto.

- Anthropic, por proporcionar acceso a Claude Code a través del
programa [Claude for Open Source](https://claude.com/contact-sales/claude-for-oss).
`tdb` fue creado casi en su totalidad con Claude Code.

- OpenAI, por proporcionar acceso a Codex a través del
programa [Codex for Open Source](https://developers.openai.com/community/codex-for-oss).

## Galería
<p align="center">
  <img src="https://github.com/AlDanial/tdb/blob/main/gallery/async_breakpoint.png" alt="at breakpoint" width="300">
  <img src="https://github.com/AlDanial/tdb/blob/main/gallery/async_task_graph.png" alt="task graph" width="300">
  <img src="https://github.com/AlDanial/tdb/blob/main/gallery/multiprocessing_process_3.png" alt="multiple processes" width="300">
  <img src="https://github.com/AlDanial/tdb/blob/main/gallery/threading_list.png" alt="thread list" width="300">
</p>

Videos:
- [tdb basics](https://youtu.be/2_qf2WZDHuA) vistas, atajos de teclado, puntos de interrupción, ejecución paso a paso, modificación de variables, pila de llamadas
- [asyncio tasks](https://youtu.be/vM4tODuqMGg) inspeccionar tareas de asyncio y su gráfico de espera; modificación de código para permitir pausa
- [threads and processes](https://youtu.be/J8LOARLs2oQ) inspeccionar variables y pilas de llamadas en múltiples hilos y procesos
- [external terminal](https://youtu.be/121aihjAQ8g) ejecutar el depurado en una terminal separada, ideal para depurar aplicaciones TUI

## Instalación

```bash
pip install textual-debugger
```

o (mejor):

```bash
uv pip install textual-debugger
```

o ejecutarlo sin instalar:

```
uvx --from textual-debugger tdb  my_program.py
```


## Inicio Rápido

```bash
# mostrar documentación completa en un visor de Markdown basado en terminal
tdb --doc

# depurar un script (detiene por defecto en la primera línea)
tdb my_program.py

# depurar con argumentos
tdb my_program.py arg1 arg2

# depurar un ejecutable nativo C/C++ (u otro) compilado con -g. El binario ELF/Mach-O/PE
# se detecta automáticamente y se depura a través del modo DAP de GDB (GDB >= 14)
tdb ./myprog arg1 arg2

# lo mismo, pero usando lldb-dap (LLVM >= 17) en lugar de gdb
tdb --adapter lldb-dap ./myprog

# forzar el lenguaje cuando la detección automática no puede determinar (ej. un script sin extensión)
tdb --lang python ./mytool

# agregar puntos de interrupción en las líneas 20 y 35 de `my_program.py` y la línea 14
# de `module.py` (cuando se pasa -k, se establece --no-stop-on-entry y el
# programa se ejecuta hasta el primer punto de interrupción)
tdb -k 20 -k 35 -k module.py:14 my_program.py arg1 arg2

# ejecutar directamente hasta la línea 20 sin guardar el punto de interrupción para
# sesiones futuras (-t es -k menos la persistencia)
tdb -t 20 my_program.py

# usar un virtualenv específico
tdb --python /path/to/venv/bin/python my_program.py

# entrar paso a paso, o detenerse en trazas en código de librería
tdb --no-just-my-code --python /path/to/venv/bin/python my_program.py

# ejecutar hasta el primer punto de interrupción o salir
tdb --no-stop-on-entry my_program.py

# ejecutar el programa depurado en una terminal externa
tdb --terminal xterm my_program.py

# adjuntar a un programa Python remoto que tenga un servidor debugpy en el puerto 5678
# (el código fuente se descarga automáticamente desde el host remoto)
tdb -r remotehost:5678

# adjuntar a un programa Python remoto que tenga un servidor debugpy en el puerto 5678
# y establecer un punto de interrupción donde tdb y el programa remoto tengan la misma
# estructura de código fuente
tdb -r remotehost:5678  -k my_program.py:42

# adjuntar a un programa Python remoto que tenga un servidor debugpy en el puerto 5678
# y establecer un punto de interrupción donde el código en el host local esté en una ubicación diferente
# que el código en el host remoto
tdb -r remotehost:5678 --local-root /my/code/dir --remote-root /app -k my_program.py:42

# separar argumentos de tdb de argumentos del depurado con `--` 
tdb --python /path/to/venv/bin/python -- my_program.py -k 17 --max 23.3
```

Alternativamente, usa el punto de entrada del módulo:

```bash
python -m tdb my_program.py
```

## Depuración Multi-Lenguaje

`tdb` depura cualquier lenguaje que tenga un backend del Debug Adapter Protocol. Dos
lenguajes son compatibles directamente:

| Lenguaje | Adaptador(es) | Cómo obtener el adaptador | Nivel de características |
|----------|------------|------------------------|---------------|
| Python | `debugpy` (por defecto) | instalado con `textual-debugger` | todo lo descrito en este README |
| C / C++ (cualquier binario nativo) | `gdb` (por defecto), `lldb-dap` (alternativo) | `gdb -i dap` requiere GDB ≥ 14; `lldb-dap` se incluye con LLVM ≥ 17 (ej. `apt install lldb`) | depuración básica: puntos de interrupción, paso a paso, pila, variables, consola de evaluación |
| Perl | perl-tdb (incluido) | necesita perl ≥ 5.18 en PATH (o `{"adapters": {"perl": ...}}`) | depuración básica + adjuntamiento remoto |

### Detección y selección de lenguaje

El lenguaje se detecta automáticamente desde el objetivo de depuración:

1. Extensión de archivo: `.py` → Python; `.pl` / `.pm` / `.t` → Perl.
2. Ejecutables nativos (bytes mágicos ELF, Mach-O, PE) → C/C++.
3. Un shebang `#!...python` o `#!...perl` → Python / Perl respectivamente.
4. Archivos *fuente* de C/C++/Rust (`.c`, `.cpp`, `.rs`, …) producen un error con un
   consejo: compila con información de depuración (`g++ -g -O0`) y depura el binario.
5. Cualquier otra cosa produce un error indicando la anulación `--lang`.

`--lang` fuerza el lenguaje; `--adapter` selecciona un adaptador no predeterminado dentro
de él (`tdb --lang cpp --adapter lldb-dap ./myprog`).

> **Nota de migración:** los scripts de Python sin extensión y sin shebang `python`
> previamente se asumían como Python; ahora requieren `--lang python`.

### Los adaptadores se encuentran en `PATH`

`tdb` no descarga ni incluye adaptadores. Si el ejecutable del adaptador no se
encuentra, el error nombra el paquete para instalar. Para usar un adaptador desde una
ubicación no estándar, o cambiar el adaptador predeterminado de un lenguaje, añade a
`config.json` (ver [Configuración](#configuration)):

```json
{
  "adapters": {"lldb-dap": "/opt/llvm/bin/lldb-dap"},
  "default_adapters": {"cpp": "lldb-dap"}
}
```

### Qué funciona para lenguajes distintos a Python

La depuración básica funciona idénticamente para cada lenguaje: puntos de interrupción (incl.
condiciones y persistencia), paso a paso, continuar/pausar, ejecutar hasta el cursor, navegación de pila,
inspección de variables, consola de evaluación, resaltado de sintaxis,
y los modos programáticos JSON-RPC / MCP.

Las características específicas de Python se ocultan o devuelven un claro mensaje de "no soportado para este
lenguaje" cuando se depuran otros lenguajes: paso a paso a nivel de instrucción (los lenguajes distintos a Python siempre dan paso por línea), los inspectores de tareas asíncronas /
procesos y gráfico de espera, la ayuda de `?` al final en la consola de evaluación,
`--python`/`--pv`, `--no-subprocess`, adjuntamiento automático de procesos hijos, y
los ganchos post-mortem / `tdb.breakpoint()` (esos ganchos viven dentro de programas Python
por naturaleza). El adjuntamiento remoto (`-r`) también funciona para Perl (ver
[Perl](#perl) — `Devel::TdbRemote` en lugar de `debugpy.listen()`), pero no
para C/C++. `--terminal` se
ignora actualmente para objetivos distintos a Python.

### Consejos para C/C++

- Compila con `-g` (idealmente `-g -O0`). Si no se puede enlazar ningún punto de interrupción en un archivo, `tdb` imprime una advertencia en la consola sugiriendo que el programa puede carecer
  de información de depuración.
- Los marcos de pila que apuntan a librerías del sistema a menudo no tienen código fuente en disco;
  la Vista de Código muestra un marcador `<Could not read …>` mientras la pila,
  variables y consola de evaluación permanecen totalmente funcionales.
- GDB (el adaptador por defecto) tiene la impresionante pretty-printing de libstdc++ más completa. `lldb-dap` (vía `--adapter lldb-dap`) también depura
  binarios compilados con GCC sin problemas — DWARF es neutral al compilador.
- **Extrañeza de la consola de evaluación de GDB:** El DAP de GDB trata la entrada REPL como comandos CLI,
  así que evalúa expresiones con un `print` explícito, ej.
  `print x` en lugar de `x` suelto (`x` suelto colisiona con el comando de examinar memoria de GDB). `lldb-dap` evalúa expresiones sueltas directamente.

### Perl

`tdb` incluye su propio adaptador Perl (`perl-tdb`) — no se necesita instalación de adaptador por separado,
solo un `perl` ≥ 5.18 en `PATH`. Controla `perl5db` nativo por debajo del capó, así que funciona con cualquier Perl ya presente en el sistema.

**Iniciar un script:**

```bash
tdb script.pl
```

**Adjuntamiento remoto:** útil cuando el proceso Perl ya está en ejecución (un servicio de larga
duración, un proceso iniciado por algo distinto a `tdb`) o reside en
otro host/contenedor. Añade tres líneas al programa objetivo, con la línea
`use` primero para que el depurador se arme antes de que cualquiera de tu código compile:

```perl
use Devel::TdbRemote;                 # PRIMERA línea de tu programa
...
Devel::TdbRemote::listen(5678);       # no bloqueante
Devel::TdbRemote::wait_for_client();  # bloquea hasta que tdb se conecte
```

Luego adjunta desde `tdb`, forzando el lenguaje ya que no hay un argumento `program` local para que `tdb` lo detecte:

```bash
tdb --lang perl -r host:5678
```

**Advertencia sobre la activación:** solo el código *compilado después* de que el depurador se arme puede ser
ejecutado paso a paso o tener puntos de interrupción. Por eso `use Devel::TdbRemote;` debe ser la
primera línea del programa. Si no puedes editar la primera línea (ej. un script wrapper controla el inicio), ármalo antes de que Perl siquiera analice tu archivo:
`perl -d:TdbRemote prog.pl`, o establece `PERL5OPT=-d:TdbRemote` en el
entorno que lanza el depurado.

**Copiar el adaptador a un host remoto:** `Devel::TdbRemote` y su script auxiliar son archivos planos, no una instalación de CPAN — copia ambos en la máquina remota y apunta `PERL5LIB` al directorio que los contiene:

```bash
# Desde un checkout o site-packages/tdb/adapters/perl de un wheel instalado:
scp -r Devel/TdbRemote.pm helpers.pl remote-host:/opt/tdb-perl/
# En el host remoto:
export PERL5LIB=/opt/tdb-perl:$PERL5LIB
```

(`Devel/TdbRemote.pm` localiza `helpers.pl` junto a sí mismo en tiempo de ejecución, así que mantén
los dos archivos en el mismo diseño relativo mostrado arriba — `helpers.pl` es
un hermano del directorio `Devel/`, no dentro de él.)

**PadWalker (opcional pero recomendado):** inspeccionar variables léxicas (`my`) en el marco *actual* siempre funciona. Léxicos en marcos externos/llamadores necesitan el módulo `PadWalker` instalado en el Perl del depurado; sin él, tdb recurre a un recorrido de pad de solo lectura que no puede llegar completamente a alcances envolventes, y las listas de variables de marcos externos se degradan en consecuencia.
Instala con `cpanm PadWalker` (o el paquete de tu distro) para fidelidad completa.

**La pausa no está disponible en modo adjuntamiento.** Las sesiones en modo lanzamiento (`tdb
script.pl`) soportan pausar un programa en ejecución en cualquier momento. Las sesiones de adjuntamiento remoto no — la pausa asíncrona estilo debugpy necesita un canal de control que `Devel::TdbRemote` aún no implementa; `pause` en modo adjuntamiento devuelve un claro error de "no disponible" en lugar de colgarse.

## Disposición

```
┌─ Header ──────────────────────────────────────────────┐
├─ Menu Bar (File / Configure / Help)───────────────────┤
│                           │                           │
│   Code View               │  Console View (stdout)    │
│   (source + breakpoints)  ├───────────────────────────┤
│                           │  Variable View (tree)     │
│                           ├───────────────────────────┤
│                           │  Stack View (call stack)  │
├─ Status Bar ──────────────────────────────────────────┤
│                           │                           │
│  Evaluate Console (REPL)  │  Breakpoint View (table)  │
│                           │                           │
├─ Footer (keybindings) ────────────────────────────────┤
└───────────────────────────────────────────────────────┘
```

La barra de estado muestra el estado de ejecución actual (ejecutando, pausado,
punto de interrupción alcanzado) y ubicación.
El pie de página muestra los atajos de teclado más relevantes para el modo actual.

## Características

### Navegación y Atajos de Teclado


La Vista de Código muestra código fuente con resaltado de sintaxis (lexer elegido por lenguaje) con números de línea.
Una línea de cursor (azul) rastrea tu posición; la línea de ejecución actual se resalta en dorado.

**Atajos de enfoque de vista (globales):**

| Tecla | Vista |
|-----|------|
| `Ctrl+C` | Vista de Código |
| `Ctrl+O` | Vista de Consola |
| `Ctrl+E` | Consola de Evaluación |
| `Ctrl+V` | Vista de Variables |
| `Ctrl+S` | Vista de Pila |
| `Ctrl+B` | Vista de Puntos de Interrupción |

**Atajos de barra de menú (globales):**

`Alt+<primera-letra>` abre la pestaña correspondiente en la barra de menú.

| Tecla | Menú |
|-----|------|
| `Alt+F` | Archivo (abrir un script diferente para depurar) |
| `Alt+C` | Configurar (Tema de Color, Atajos de Teclado, Modo de Paso) |
| `Alt+T` | Hilos |
| `Alt+P` | Procesos |
| `Alt+A` | Tareas Asíncronas |
| `Alt+H` | Ayuda (Documentación, Acerca de) |

**Navegación (estilo vim por defecto):**

Por defecto, la Vista de Código está en modo Depuración. Presiona `Escape` para cambiar a modo Navegación.
En modo Navegación, puedes moverte por el archivo con las siguientes teclas:

| Tecla | Acción |
|-----|--------|
| `j` / `k` | Mover cursor abajo / arriba |
| `5j`, `10k` | Mover N líneas abajo / arriba con prefijo de conteo |
| `G` | Ir al final del archivo (con conteo: `42G` salta a la línea 42)|
| `[` / `]` | Saltar al límite de párrafo anterior / siguiente |
| `/` | Buscar hacia adelante |
| `?` | Buscar hacia atrás |
| `n` / `N` | Siguiente / anterior resultado de búsqueda |
| `PageUp` / `PageDown` | Desplazar por página |

Vuelve a modo Depuración desde Navegación con `Escape`.

> **Nota:** Muchas terminales envían la secuencia de bytes `ESC+f` para `Alt+F`, que el analizador ANSI de Textual reescribe como `Ctrl+Derecha` (la convención "forward-word" de readline).
`tdb` vincula ambos para que `Alt+F` funcione como se espera.

### Controles de Depuración

Los atajos de teclado para paso a paso, continuar, pausar y navegación de pila coinciden
con los de gdb/pdb, con algunos alias y extras añadidos por comodidad.

| Tecla | Acción |
|-----|--------|
| `n` | Paso sobre (siguiente instrucción) |
| `s` | Paso dentro de llamada a función |
| `o` / `f` / `r` | Paso fuera de la función actual (también alias como "finish" y "return") |
| `c` | Continuar ejecución |
| `p` | Pausar un programa en ejecución |
| `t` | Ejecutar hasta la posición del cursor |
| `u` / `d` | Navegar pila arriba (llamador) / abajo (llamado) |
| `j` / `k` | Mover cursor abajo / arriba (con conteo: `5j`, `10k`) |
| `G` | Ir a la última línea (con conteo: `42G` salta a la línea 42) |
| `e` | Re-mostrar el último error (traceback) |
| `R` | Reiniciar la sesión de depuración |
| `q q` | Salir |
| `Ctrl+q` | Salir |

> **Nota:** `f` ("finish") y `r` ("return") son ambos alias para paso fuera. El único
primitivo "salir-de-una-función" de DAP es `stepOut`, que ejecuta el resto de la función actual
normalmente y se detiene en el punto de retorno. Un retorno inmediato estilo gdb (saltando
código restante en la función sin ejecutar efectos secundarios) no es soportado por DAP/debugpy.

**Granularidad de paso (instrucción vs. línea):** por defecto, `n` (paso sobre) y `s` (paso dentro)
tratan una instrucción de código fuente multi-línea como un solo paso. Por ejemplo, dar paso sobre

```python
results = await asyncio.gather(
    fetch(1, 2),
    fetch(2, 1),
    fetch(3, 3),
)
print(results)   # la próxima parada cae aquí, no en cada sub-línea interna arriba
```

cae en `print(results)`, no en cada sub-línea interna de la llamada `gather`. Cambia a
modo **Línea** (Configurar > Modo de Paso) para obtener el comportamiento nativo por línea de debugpy, que
se detiene en cada línea física — útil para inspeccionar cómo se construye una expresión compleja.
La elección se guarda en `~/.config/tdb/config.json`.

El modo instrucción requiere un modelo de lenguaje fuente y actualmente es solo para Python;
otros lenguajes siempre dan paso por línea (el menú Modo de Paso lo indica si intentas cambiarlo).

### Puntos de Interrupción

Haz clic en el margen en la Vista de Código para alternar un punto de interrupción, o presiona `b` en modo Depuración.

**Indicadores de puntos de interrupción:**
- Punto rojo: punto de interrupción activo
- Punto amarillo: punto de interrupción condicional
- Punto azul: punto de interrupción desactivado

**Puntos de interrupción condicionales:** Haz doble clic en un punto de interrupción para abrir el editor de condiciones.
Establece una expresión Python (ej., `x > 10`) y/o un conteo de hits (pausar en el N-ésimo hit).

**Acciones de la Vista de Puntos de Interrupción:**
- `D` : Desactivar / activar todos los puntos de interrupción
- `C` : Limpiar todos los puntos de interrupción

Los puntos de interrupción persisten entre reinicios de sesión.

### Inspección de Variables

La Vista de Variables muestra un árbol de ámbitos (Locales, Globales) con todas las variables en el marco actual.
Expande nodos para profundizar en objetos complejos. Los hijos se cargan perezosamente bajo demanda.
Los valores de variables pueden cambiarse en la Consola de Evaluación.

Haz doble clic en una variable, o resalta la variable con el cursor de texto en
la Vista de Variables y presiona `Enter`
para mostrar esa variable en un modal. Esto simplifica la inspección de
estructuras de datos grandes o profundamente anidadas.

### Pila de Llamadas

La Vista de Pila muestra la pila de llamadas completa. Haz clic en un marco para navegar a su ubicación de código fuente
e inspeccionar sus variables.

### Consola de Evaluación

Un ciclo leer-evaluar- imprimir (REPL) en la parte inferior izquierda permite
la evaluación interactiva de expresiones en el ámbito actual:

```
>>> len(items)
42
>>> sorted(data, key=lambda x: x.priority)[:3]
[Item(priority=1), Item(priority=2), Item(priority=3)]
```

- **Flechas Arriba/Abajo** ciclan por el historial de expresiones
- **Tab** activa completado basado en DAP
- **`?` al final** muestra ayuda (signatura + docstring):

```
>>> os.path.join?
(a, *p) : Une dos o más componentes de ruta...
```

Los valores de variables establecidos aquí se reflejan en el código en ejecución.

### Cortar / Pegar

Las expresiones para la Consola de Evaluación a menudo se copian desde la Vista de Código.
Hacer esto en `tdb` difiere del comportamiento tradicional de terminal, porque las aplicaciones `textual`
capturan eventos de mouse para su propio uso.

En su lugar, mantén la tecla `Shift` mientras realizas tus teclas de cortar/pegar convencionales o operación de mouse
para obtener el comportamiento esperado.

### Salida de Consola

La Vista de Consola captura stdout (texto normal) y stderr (texto rojo) del depurado en tiempo real.

Si tu programa imprime mucho, o solicita entrada, o usa colores o
códigos de control de terminal, ejecuta el programa en una terminal externa
con `--terminal` para una mejor experiencia.
El conmutador `--terminal` requiere un entorno gráfico y un emulador de terminal compatible.


### Detección de Fallos

Cuando el depurado lanza una excepción no manejada, `tdb`:
1. Muestra un modal con la traza completa
2. Navega la Vista de Código a la línea del fallo
3. Poblala la Vista de Pila con la pila de llamadas de la excepción
4. Te permite presionar `R` para reiniciar o `Escape` para descartar

> Nota: después de descartar el modal de la traza, puedes
> volver a mostrarlo presionando `e` cuando el enfoque esté en la Vista de Código.

### Gancho de Excepción Post-Mortem

Puedes hacer que `tdb` se abra automáticamente cuando *cualquier* programa Python falle sin la
necesidad de lanzarlo a través de `tdb` inicialmente. Instala el gancho una vez al inicio de tu programa:

```python
import sys
import tdb
sys.excepthook = tdb.exception_hook
```

Cuando una excepción no capturada alcanza el gancho, `tdb`:

1. Imprime la traza estándar de Python a stderr (para que tu scrollback aún tenga un registro)
2. Captura una instantánea de cada marco en la traza. Esto incluye locales, más un nivel de
recursión en contenedores (`dict`, `list`, `tuple`, `set`) y objetos con `__dict__`
3. Lanza la TUI en **modo post-mortem**, heredando la terminal actual

En modo post-mortem puedes:

- Navegar la pila de llamadas (`u` / `d` o la Vista de Pila) y ver los locales de cada marco
- Expandir contenedores anidados y atributos de objetos en la Vista de Variables
- Leer la traza completa (incluyendo excepciones encadenadas `cause`/`context`) en la Vista de Consola
- Moverse por el código fuente con la Vista de Código completa (resaltado de sintaxis, ir a línea, etc.)

El paso a paso, continuar, puntos de interrupción, reinicio y la Vista de Evaluación están deshabilitados. El intérprete original
ya no existe ya que la vista es una instantánea congelada. Presiona `q` para salir.

El gancho es una operación nula cuando stdin/stdout no son un tty (ej. cuando tu programa está en pipe o
ejecutado desde cron), por lo que es seguro dejarlo instalado en código de producción. Las instantáneas se
escriben en un archivo temporal que se elimina tan pronto como `tdb` sale.

La profundidad/amplitud de la instantánea está limitada (5 niveles, 50 hijos por contenedor) para mantener la
captura económica incluso para grafos de objetos patológicos; los ciclos se manejan mediante memorización de identidad.

### Post-Mortem dentro de un Contenedor Docker

El directorio `examples/` del repositorio GitHub de `textual-debugger` tiene tres archivos
que muestran cómo ejecutar un programa Python habilitado para `tdb` bajo `tmux` en un contenedor Docker
para que puedas adjuntarte al contenedor e inspeccionar el programa
en modo de análisis post-mortem de `tdb` si el programa encuentra una excepción no manejada:

- [post_mortem_example.py](https://github.com/AlDanial/tdb/blob/main/examples/post_mortem_example.py)
- [post_mortem_entrypoint.sh](https://github.com/AlDanial/tdb/blob/main/examples/post_mortem_entrypoint.sh)
- [Dockerfile.post_mortem](https://github.com/AlDanial/tdb/blob/main/examples/Dockerfile.post_mortem)

### Gancho de Punto de Interrupción en Vivo

`tdb` tiene una implementación mejorada de la función estándar `breakpoint()` (o equivalentemente,
`pdb.set_trace()`) usada para pausar en una línea específica para inspeccionar, luego
continuar — usa `tdb.breakpoint()`:

```python
import tdb

def compute(n):
    total = sum(range(n))
    tdb.breakpoint()  # pausar aquí y entrar en tdb
    return total
```

O enlácelo a la función integrada `breakpoint()` para todo el programa:

```bash
PYTHONBREAKPOINT=tdb.breakpoint python myscript.py
```

Cuando se alcanza la llamada, `tdb` inicia un servidor `debugpy` en proceso en un puerto de loopback,
lanza `python -m tdb -r <port>` como un subproceso para que la TUI tome el control de la terminal,
y pausa el hilo llamador en la línea que llamó `tdb.breakpoint()` (el gancho
auto-pasa fuera de su propio auxiliar para que aterrices en tu propio marco, no dentro
de `breakpoint_hook.py`). El paso a paso (`n`/`s`/`o`), `continue`, y establecer/eliminar puntos de interrupción
funcionan normalmente; salir de `tdb` (`Ctrl+q`) desadjunta sin matar el programa, y
debugpy reanuda automáticamente cualquier hilo aún pausado.

Esto difiere de `tdb.exception_hook` en una forma:
- **Requiere `debugpy`** como dependencia en tiempo de ejecución para el depurado (solo se importa cuando el gancho realmente se activa).

A diferencia del gancho de excepción (que funciona en una instantánea congelada), el gancho de punto de interrupción deja
al intérprete en vivo: la inspección de variables lee objetos reales, y el paso a paso/`continue`
impulsan el programa del usuario hacia adelante.

Al igual que con `exception_hook`, la llamada es una operación nula cuando stdin/stdout no son un tty, por lo que es
seguro dejarlo en rutas de código que a veces se ejecutan sin interfaz.

Salir de `tdb` mientras se está pausado en una sesión `tdb.breakpoint()` desadjunta el depurador y
permite que el programa continúe ejecutándose normalmente.
Este comportamiento coincide con presionar `c` mientras estás en una sesión `breakpoint()` convencional (es decir, la de la biblioteca estándar de Python).
Si quieres matar el programa en su lugar, usa `Ctrl+c` en la terminal que ejecuta el depurado.

### Inspector de Tareas Asíncronas

Para programas que usan `asyncio`, la barra de menú muestra una etiqueta **Async Tasks (N)** con el conteo de
tareas activas (actualizada cada vez que el programa se detiene). Haz clic para abrir un modal a pantalla completa:

- **Panel izquierdo**: lista de todas las tareas con nombre, estado (pendiente/hecho/cancelado), primitiva en espera
  (`Lock.acquire`, `Queue.get`, `asyncio.sleep`, …), y coroutine
- **Panel derecho**: vista de detalle con traza de pila completa y árbol de variables expandible (igual
que la Vista de Variables principal) para la tarea seleccionada
- Presiona `g` para cambiar el panel derecho al **gráfico de espera** que es un árbol mostrando
  cada tarea bloqueada, la primitiva de asyncio en la que está estacionada, y la(s) tarea(s) que sostienen esa primitiva.
  Los ciclos (deadlocks) se resaltan en rojo tanto en la tabla de tareas como en una sección "Ciclos de deadlock"
  en la parte superior del gráfico. Seleccionar un nodo en el árbol resalta la
  tarea correspondiente en la tabla.
- Navega con las teclas de flecha; presiona `r` para actualizar, `Escape` para cerrar

> Nota: las relaciones de tareas asíncronas pueden ser grafos acíclicos dirigidos (DAGs) en lugar de árboles,
> pero no conozco una forma de visualizar DAGs en textual.

Equivalentes RPC:

```bash
# Listar todas las tareas
curl -s -X POST http://127.0.0.1:8150/rpc \
  -H 'Content-Type: application/json' \
  -d '{"action":"list_tasks","params":[]}'

# Inspeccionar una tarea específica por nombre
curl -s -X POST http://127.0.0.1:8150/rpc \
  -H 'Content-Type: application/json' \
  -d '{"action":"inspect_task","params":["Task-1"]}'

# Mostrar gráfico de espera y cualquier ciclo de deadlock
curl -s -X POST http://127.0.0.1:8150/rpc \
  -H 'Content-Type: application/json' \
  -d '{"action":"wait_graph","params":[]}'
```

### Inspector de Hilos

La barra de menú muestra una etiqueta **Threads (N)** cuando el programa tiene 2 o más hilos. Haz clic para abrir un modal con:

- **Panel izquierdo**: lista de hilos con ID y nombre (hilo actual mostrado en negrita)
- **Panel derecho**: traza de pila completa y árbol de variables expandible para el marco superior del hilo seleccionado
- Navega con las teclas de flecha; presiona `r` para actualizar, `Escape` para cerrar

Equivalentes RPC:

```bash
# Listar todos los hilos (* marca el actual)
curl -s -X POST http://127.0.0.1:8150/rpc \
  -H 'Content-Type: application/json' \
  -d '{"action":"list_threads","params":[]}'

# Inspeccionar un hilo específico por ID
curl -s -X POST http://127.0.0.1:8150/rpc \
  -H 'Content-Type: application/json' \
  -d '{"action":"inspect_thread","params":[1]}'
```

### Inspector de Procesos

Para programas que usan `multiprocessing`, la barra de menú muestra una etiqueta **Processes (N)** cuando hay
2 o más procesos hijos. Haz clic para abrir un modal con:

- **Panel izquierdo**: lista de procesos hijos con PID, nombre y estado (vivo/salido)
- **Panel derecho**: detalles del proceso, traza de pila completa y árbol de variables expandible para el proceso seleccionado

`tdb` se adjunta automáticamente a procesos hijos lanzados vía `multiprocessing.Process`, `multiprocessing.Pool`,
o `concurrent.futures.ProcessPoolExecutor`. Los puntos de interrupción establecidos en el padre se propagan a todos
los procesos hijos. Cuando cualquier proceso alcanza un punto de interrupción, todos los demás procesos se pausan. Presionar `p`
pausa todos los procesos; `c` continúa todos.

**Paso a paso en programas multiproceso:** los comandos de paso (`n`, `s`, `o`, `f`, `r`) se aplican solo al
proceso cuya pila se muestra actualmente en la Vista de Código (el que alcanzó el punto de interrupción).
Otros procesos permanecen pausados durante todo el paso. Para dar paso en un proceso diferente, abre
la pestaña Procesos y selecciónalo primero. La Vista de Código luego cambia el enfoque a ese proceso,
y los comandos de paso posteriores operan sobre él.

Equivalentes RPC:

```bash
# Listar todos los procesos hijos
curl -s -X POST http://127.0.0.1:8150/rpc \
  -H 'Content-Type: application/json' \
  -d '{"action":"list_processes","params":[]}'

# Inspeccionar un proceso específico por nombre o PID
curl -s -X POST http://127.0.0.1:8150/rpc \
  -H 'Content-Type: application/json' \
  -d '{"action":"inspect_process","params":["ForkPoolWorker-1"]}'
```

### Adjuntamiento Remoto

El adjuntamiento remoto es útil en situaciones donde no puedes lanzar el depurado directamente
con `tdb`, por ejemplo, si es lanzado desde otro programa o ejecuta en un entorno
donde no puedes instalar `tdb`. Sin embargo, deben cumplirse dos requisitos:
1. el paquete `debugpy` debe estar instalado en el entorno Python del depurado
2. necesitas acceso de escritura al código del depurado para añadir el siguiente código en el punto
donde quieres adjuntar el depurador:

```python
# En el programa objetivo:
import debugpy
debugpy.listen(("0.0.0.0", 5678))
print("Esperando a que tdb se adjunte en el puerto 5678...")
debugpy.wait_for_client()  # opcional: pausar hasta que el depurador se conecte
print("¡tdb está adjuntado!")
```

Cuando el depurado ejecuta y alcanza la línea `debugpy.wait_for_client()`, inicia un
servidor debugpy escuchando en el puerto 5678.
Adjunta `tdb` a él con el conmutador `-r`, especificando el host y el puerto.
Si el depurado está en la misma máquina, puedes omitir el host o usar `localhost`.
Este ejemplo asume que el depurado ejecuta en 192.168.1.10 y escucha en el puerto 5678:

```bash
# Adjuntar desde tdb:
tdb -r 5678   # a localhost
tdb -r 192.168.1.10:5678

# Con puntos de interrupción:
tdb -r 5678 -k my_program.py:42
```

Todas las características de depuración (puntos de interrupción, paso a paso, inspección de variables, hilos, procesos,
tareas asíncronas) funcionan en modo adjuntamiento remoto. La Vista de Código navega automáticamente al
archivo de código fuente cuando el programa se detiene.

**Mapeo de rutas remotas a copias locales (`--local-root` / `--remote-root`):** cuando el
depurado reside en otra máquina, o en un contenedor, o simplemente en un directorio diferente
en la misma máquina, las rutas de código fuente que informa (y las rutas a las que espera que los puntos de interrupción
se refieran) no coincidirán con nada en el host de `tdb`. Para cerrar esa brecha, dale a `tdb` uno
o más pares `--local-root` / `--remote-root`. Cada `--local-root` apunta a un directorio local
que contiene una copia del código; cada `--remote-root` es la ruta correspondiente
en el depurado. Las dos banderas deben suministrarse en números iguales y emparejarse en orden CLI
vía `zip()`, así que el primer `--local-root` coincide con el primer `--remote-root`, el
segundo coincide con el segundo, y así sucesivamente. `debugpy` luego traduce rutas bidireccionalmente:
los puntos de interrupción establecidos en un archivo local caen en el archivo remoto coincidente, y las rutas de código fuente
devueltas en eventos de parada / trazas de pila se reescriben de vuelta a la copia local para que la
Vista de Código cargue directamente desde disco (sin ida y vuelta de `source` DAP necesaria).

Estas banderas son requeridas siempre que quieras establecer un punto de interrupción `-k` contra un
depurado remoto cuyo código reside en una ruta diferente a tu copia local. Por ejemplo, si el
remoto ejecuta `program.py` en `/path/to/code/program.py` y tu copia local está en
`/local/project/code/program.py`, establece un punto de interrupción en la línea 321 con:

```bash
tdb -r RHOST:15678 \
    --local-root /local/project/code \
    --remote-root /path/to/code \
    -k program.py:321
```

Con `--local-root` establecido, un `-k FILE:LINE` relativo se resuelve buscando en cada
directorio `--local-root` en orden CLI (la primera coincidencia gana); las rutas absolutas aún funcionan como
antes. Se pueden suministrar múltiples pares para espejar múltiples árboles de código fuente (ej. un
directorio de aplicación y un directorio de librería compartida) en una sola invocación.

### Soporte para Terminal Externa

Algunos programas Python, notablemente interfaces de usuario de texto, usan códigos de control de terminal
y requieren acceso directo a la terminal para funcionar correctamente. 
Esos programas pueden depurarse con `tdb` haciéndolo lanzar el depurado en
una terminal separada:

```bash
tdb --terminal xterm my_tui_app.py
```

El depurado ejecuta en una ventana separada de la terminal especificada. Opciones soportadas:
`xterm`, `konsole`, `gnome-terminal`, `ghostty`, `kitty`, `iterm2`, `warp`,
`wezterm`, `terminator`. La terminal seleccionada debe estar en `PATH`. La depuración
procede como de costumbre en la terminal donde se invocó `tdb`.

Esta característica solo funciona en entornos gráficos donde las terminales externas están disponibles.

### Esquemas de Atajos de Teclado

```bash
tdb --keybindings vim my_program.py    # por defecto
tdb --keybindings emacs my_program.py
tdb --keybindings default my_program.py
```

La elección de atajos de teclado se guarda en `~/.config/tdb/config.json` y se recuerda para ejecuciones posteriores.
Consulta la referencia completa de atajos de teclado desde el menú: **Configurar > Atajos de Teclado**.

## Modo Servidor JSON-RPC

`tdb` incluye un servidor de depuración integrado para control programático que es útil para depuración scriptada,
pipelines de CI, o flujos de trabajo de depuración asistida por IA.

### Modo Headless (sin TUI)

```bash
python -m tdb --headless my_program.py &
```

El servidor escucha en `http://127.0.0.1:8150/rpc` (cambia con `--server-port`).

### Modo Dual (TUI + servidor)

```bash
tdb --server my_program.py
```

Tanto la TUI interactiva como el servidor JSON-RPC ejecutan simultáneamente.

### Protocolo RPC

Envía solicitudes POST con `{"action": "...", "params": [...]}`. Las respuestas devuelven
`{"timestamp": "...", "success": true/false, "value": "..."}`.

```bash
# Verificar estado
curl -s -X POST http://127.0.0.1:8150/rpc \
  -H 'Content-Type: application/json' \
  -d '{"action":"status","params":[]}'

# Establecer un punto de interrupción
curl -s -X POST http://127.0.0.1:8150/rpc \
  -H 'Content-Type: application/json' \
  -d '{"action":"set_breakpoint","params":["/abs/path/to/file.py:42"]}'

# Continuar ejecución
curl -s -X POST http://127.0.0.1:8150/rpc \
  -H 'Content-Type: application/json' \
  -d '{"action":"continue","params":[]}'

# Inspeccionar variables
curl -s -X POST http://127.0.0.1:8150/rpc \
  -H 'Content-Type: application/json' \
  -d '{"action":"inspect","params":["x", "len(items)", "type(result)"]}'

# Apagar
curl -s -X POST http://127.0.0.1:8150/rpc \
  -H 'Content-Type: application/json' \
  -d '{"action":"quit","params":[]}'
```

### Todas las Acciones RPC

| Acción | Parámetros | Descripción |
|--------|--------|-------------|
| `help` | `[]` | Listar todas las acciones |
| `status` | `[]` | Estado actual con ubicación |
| `set_breakpoint` | `["file:line"]` o `["file:line", "condition", "hit_condition"]` | Establecer un punto de interrupción |
| `remove_breakpoint` | `["file:line"]` | Eliminar un punto de interrupción |
| `list_breakpoints` | `[]` | Mostrar todos los puntos de interrupción |
| `continue` | `[]` o `[timeout_s]` | Reanudar ejecución; al tiempo agotado devuelve `"still running--call pause or wait again"` (éxito) |
| `next` | `[]` o `[timeout_s]` | Paso sobre |
| `step_in` | `[]` o `[timeout_s]` | Paso dentro |
| `step_out` | `[]` o `[timeout_s]` | Paso fuera |
| `pause` | `[]` | Pausar ejecución; omite el bloqueo de despacho para que pueda interrumpir una acción bloqueante en vuelo |
| `wait_for_stop` | `[]` o `[timeout_s]` | Esperar la próxima parada sin emitir un paso (usa después de que `continue` devuelva `"still running"` para seguir esperando) |
| `inspect` | `["expr1", "expr2", ...]` | Evaluar múltiples expresiones |
| `evaluate` | `["expression"]` | Evaluar una sola expresión |
| `stack_up` | `[]` | Moverse arriba en la pila de llamadas |
| `stack_down` | `[]` | Moverse abajo en la pila de llamadas |
| `get_stack_trace` | `[]` | Pila de llamadas completa |
| `get_output` | `[]` | Vaciar stdout/stderr en búfer |
| `get_source` | `["file_path"]` | Leer un archivo de código fuente |
| `list_threads` | `[]` | Listar todos los hilos |
| `inspect_thread` | `[thread_id]` | Inspeccionar un hilo específico |
| `list_processes` | `[]` | Listar procesos hijos (multiprocessing) |
| `inspect_process` | `["name_or_pid"]` | Inspeccionar un proceso hijo específico |
| `list_tasks` | `[]` | Listar todas las tareas asyncio |
| `inspect_task` | `["task_name"]` | Inspeccionar una tarea asyncio específica |
| `wait_graph` | `[]` | Mostrar gráfico de espera + cualquier ciclo de deadlock |
| `restart` | `[]` | Reiniciar sesión (preserva puntos de interrupción) |
| `quit` | `[]` | Apagar |

### Flujo de Eventos SSE

Suscríbete a eventos de depuración en tiempo real:

```bash
curl -N http://127.0.0.1:8150/events
```

Eventos: `initialized`, `stopped`, `continued`, `terminated`, `exited`, `output`.
Cada uno es JSON con campos `event`, `data`, y `timestamp`.

## Grabación y reproducción de sesiones

`tdb --record session.jsonl prog.py` ejecuta una sesión TUI normal y captura
tus acciones de depuración — puntos de interrupción (incluyendo `-k`/`-t` y los persistentes),
paso a paso, continuar/pausar, entradas de la consola de evaluación, navegación de marcos de pila,
expansión de variables, reinicio, salida — a `session.jsonl` como
comandos JSON-RPC. Funciona con modo lanzamiento (cualquier lenguaje) y adjuntamiento
remoto `-r`.

Reprodúcelo de dos formas:

- `tdb --replay session.jsonl` — un comando: lanza el programa
  grabado en modo headless, alimenta cada comando grabado a través del mismo despacho RPC
  que usa `tdb --server`, e imprime una transcripción (tiempo grabado,
  comando, resultado literal, salida del programa intercalada). Código de salida 0 si y solo si
  cada comando tuvo éxito. Añade `--timing` para reproducir el ritmo original, `--replay-timeout S` para limitar cada espera de parada (30 s por defecto).
- Contra un servidor en vivo: inicia `tdb --server prog.py`, luego alimenta desde la línea 2
  en adelante del archivo a `POST /rpc` — cada línea ya es un
  cuerpo de solicitud válido:

      tail -n +2 session.jsonl | while read line; do
          curl -s -X POST -H 'Content-Type: application/json' \
               -d "$line" http://127.0.0.1:8150/rpc
      done

  (En Windows, un ciclo equivalente en Python: lee el archivo, omite la
  primera línea, `requests.post` cada línea restante.)

No se captura: visualización pura (desplazamiento, búsqueda, modales, listas de hilos/tareas),
alternadores de habilitar/deshabilitar puntos de interrupción, expansiones de variables cuando el
adaptador no informa `evaluateName` (actualmente el adaptador Perl), y
cambios de programa Archivo > Abrir.

## Integración MCP

tdb incluye un servidor Model Context Protocol (MCP) (`tdb-mcp`) que expone
el depurador como un conjunto curado de herramientas que un agente IA puede llamar. El servidor MCP
es un tercer consumidor en proceso de los mismos controladores de despacho que la
TUI y el servidor HTTP usan, así que un agente obtiene la misma semántica de bloqueo,
incluyendo la omisión de pausa-durante-continuar, y la misma superficie de inspección respaldada por DAP.

Para un ejemplo práctico de extremo a extremo (solicitar a un agente que encuentre dos
errores en tiempo de ejecución en un programa de muestra) consulta
[docs/tutorial-mcp-debugging.md](docs/tutorial-mcp-debugging.md) y
su acompañante `examples/sales_report_buggy.py`.

### Ejecutar el servidor MCP

Configura tu cliente MCP (Claude Desktop, una extensión IDE, etc.) para
lanzar `tdb-mcp` sobre stdio. Ejemplo `claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "tdb": {
      "command": "tdb-mcp"
    }
  }
}
```

Tres formas de invocación equivalentes: `tdb-mcp` (el punto de entrada dedicado),
`tdb --mcp` (el CLI principal con el conmutador `--mcp`), y
`python -m tdb.mcp` (forma módulo). Elige la que coincida con cómo tu cliente MCP
espera lanzar servidores.

### Superficie de herramientas (16 herramientas, curadas)

| Cúmulo | Herramientas |
|---------|-------|
| Ciclo de vida | `debug_launch`, `debug_attach`, `quit` |
| Control | `control(action, timeout_s=30)` — `action ∈ {continue, next, step_in, step_out, pause, wait_for_stop}` |
| Inspección | `inspect(expressions)`, `read_source(file_path)`, `stack_trace()`, `status()`, `get_output()` |
| Puntos de interrupción | `set_breakpoint(spec, condition?, hit_condition?)`, `remove_breakpoint(spec)`, `list_breakpoints()` |
| Diferenciadores | `threads(thread_id?)`, `tasks(task_name?)`, `processes(name_or_pid?)`, `wait_graph()` |

`control` es intencionalmente una herramienta que toma un enum de acción. Las seis
acciones RPC subyacentes comparten una forma de retorno, y los agentes rinden
mediblemente mejor con una superficie pequeña que con una herramienta por acción.
`threads` / `tasks` / `processes` sobrecargan lista-vs-inspección vía un
solo argumento opcional por la misma razón.

`debug_launch` acepta parámetros opcionales `lang` y `adapter` que espejan el
CLI `--lang`/`--adapter`; cuando se omiten, el lenguaje se detecta automáticamente desde
`program`, así que un agente puede pasarle un binario compilado directamente. Las
herramientas `tasks`/`processes`/`wait_graph` permanecen registradas para cada lenguaje pero
devuelven un error estructurado estilo "no soportado al depurar C/C++" para
depurados no Python.

### Flujo del agente para un paso de larga duración

```
agente → control(action="continue", timeout_s=30)
mcp   → "still running, call pause or wait again"
agente → control(action="pause")        # O: control(action="wait_for_stop", timeout_s=30)
mcp   → "<file>:<line>"
agente → inspect(["x", "len(buf)"])
mcp   → "x = 7\nlen(buf) = 1024"
```

`pause` omite el bloqueo de despacho para que pueda interrumpir un `continue`
que aún está en vuelo (HTTP y MCP comparten la misma política `NO_LOCK_ACTIONS`; ver `tdb/server/app.py`).

### Advertencia de seguridad

`inspect` llama a `evaluate` de debugpy, que es **ejecución de Python arbitraria
en el proceso del depurado**. Esto es inherente a un depurador y
no una preocupación específica de tdb, pero los clientes MCP (y los humanos que los ejecutan)
deberían aplicar modelos de permisos apropiados: no auto-aprobando
`inspect` contra expresiones no confiables, y no exponer `tdb-mcp` en
una red (transporte stdio solo por diseño).

### Diferido / fuera de alcance (v1)

- Empuje de eventos estilo SSE: `control` y `wait_for_stop` hacen la encuesta
  lo suficientemente eficiente; los eventos también necesitarían soporte desigual de clientes MCP.
- Transportes HTTP / HTTP streamable: requerirían autenticación (que el
  servidor RPC HTTP también carece actualmente); stdio hereda la confianza del
  proceso que lo lanzó.
- Multi-sesión: una sesión de depuración por proceso MCP.

## Referencia CLI

```
usage: tdb [-h] [-v] [-r [HOST:]PORT] [--cwd CWD] [--no-stop-on-entry]
           [--no-just-my-code] [--no-subprocess] [--python PYTHON] [--pv]
           [--lang LANGUAGE] [--adapter ADAPTER]
           [--keybindings {default,vim,emacs}]
           [--terminal {xterm,konsole,gnome-terminal,ghostty,kitty,iterm2,warp,wezterm,terminator}]
           [--local-root PATH] [--remote-root PATH]
           [--server] [--headless] [-k FILE:LINE|LINE] [--server-port SERVER_PORT] [-d] [--doc-text]
           [program] [args ...]
```

| Bandera | Descripción |
|------|-------------|
| `-r HOST:PORT` | Adjuntar a un servidor debugpy remoto |
| `--local-root PATH` | Directorio local que contiene una copia del código remoto (repítelo para espejar múltiples árboles). Empareja con `--remote-root`; los conteos deben coincidir. Requerido cuando `-k` establece un punto de interrupción contra un depurado remoto cuyo código reside en una ruta diferente. |
| `--remote-root PATH` | Directorio remoto emparejado con `--local-root` (misma posición CLI vía `zip()`). |
| `-k`, `--breakpoint FILE:LINE|LINE` | Establecer un punto de interrupción (puede repetirse). Pasar `-k` implica `--no-stop-on-entry` para que el programa ejecute directamente hasta el primer punto de interrupción. |
| `-t`, `--to-line FILE:LINE|LINE` | Como `-k`, pero el punto de interrupción no se guarda en el archivo de puntos de interrupción — solo te lleva a ese punto en el código para esta sesión (puede repetirse). |
| `--no-stop-on-entry` | No pausar en la primera línea (por defecto: detenerse en entrada; automático cuando se pasa `-k`) |
| `--cwd DIR` | Directorio de trabajo para el depurado |
| `--python PATH` | Intérprete Python para el depurado (solo objetivos Python) |
| `--pv` | Abreviatura para --python .venv/bin/python |
| `--lang LANGUAGE` | Lenguaje del depurado (`python`, `cpp`, `perl`); por defecto: detectar automáticamente desde el objetivo |
| `--adapter ADAPTER` | Adaptador de depuración dentro del lenguaje (ej. `--lang cpp --adapter lldb-dap`); por defecto: el adaptador estándar del lenguaje |
| `--no-just-my-code` | Paso dentro de código stdlib/site-packages en lugar de saltarlo
  (por defecto: saltado). En excepciones no capturadas, el modal de fallo siempre muestra la traza completa
  incluyendo marcos de librería, independientemente de esta bandera. |
| `--no-subprocess` | Deshabilitar el rastreo de subprocesos de debugpy (usa al depurar `tdb` mismo) |
| `--terminal TERM` | Ejecutar depurado en la terminal externa nombrada: `xterm`, `konsole`,
  `gnome-terminal`, `ghostty`, `kitty`, `iterm2`, `warp`, `wezterm`, o `terminator` |
| `--keybindings SCHEME` | `default`, `vim`, o `emacs` (guardado en config) |
| `--server` | Habilitar servidor JSON-RPC junto con TUI |
| `--headless` | Solo servidor JSON-RPC, sin TUI |
| `--server-port PORT` | Puerto del servidor (por defecto: 8150) |

## Configuración

En sistemas tipo UNIX (Linux, macOS, FreeBSD, etc.),
`tdb` almacena configuración y puntos de interrupción en `~/.config/tdb/`.
En Windows, usa `%APPDATA%\tdb\`.

| Archivo | Contenidos |
|------|----------|
| `config.json` | Preferencias de usuario (esquema de atajos, tema de color, modo de paso, anulaciones de adaptador) |
| `breakpoints.json` | Puntos de interrupción de sesiones anteriores, indexados por directorio de proyecto |

Llaves relacionadas con adaptadores en `config.json`: `adapters` mapea un ID de adaptador a una
ruta ejecutable (`{"adapters": {"lldb-dap": "/opt/llvm/bin/lldb-dap"}}`), y
`default_adapters` selecciona el adaptador predeterminado de un lenguaje
(`{"default_adapters": {"cpp": "lldb-dap"}}`).

**Perl es un caso especial:** `perl-tdb` es el adaptador empaquetado propio de tdb (siempre
encontrado — es código Python, no un ejecutable externo), así que
`adapters.perl` no selecciona un binario de adaptador. En su lugar, nombra al
*intérprete Perl* que tdb debería lanzar para ejecutar el depurado:
`{"adapters": {"perl": "/path/to/perl"}}`. Úsalo cuando el `perl` en
`PATH` es demasiado antiguo (< 5.18) o necesitas una versión específica `perlbrew`/`plenv`.

Los puntos de interrupción se guardan al salir y se restauran al depurar un programa en el mismo
directorio. Los puntos de interrupción de cada proyecto son independientes. Los puntos de interrupción establecidos con
`-t`/`--to-line` son la excepción: se comportan como puntos de interrupción `-k` durante
la sesión pero nunca se guardan.

**Modo de paso** (`step_mode` en `config.json`) controla cómo `n` (paso sobre) y `s` (paso dentro)
manejan instrucciones de código fuente multi-línea:

| Valor | Comportamiento |
|-------|----------|
| `"statement"` (por defecto) | Una instrucción multi-línea (ej. una llamada `gather(...)` que abarca cinco líneas) es un solo paso. El depurador sigue emitiendo pasos DAP hasta que la ejecución sale de la instrucción, luego se detiene en la siguiente línea lógica. |
| `"line"` | Comportamiento nativo por línea de debugpy (se detiene en cada línea física, incluyendo cada sub-línea interna de una expresión multi-línea)|

Cámbialo desde el menú (**Configurar > Modo de Paso**); la elección se guarda inmediatamente y
se aplica a todas las sesiones futuras. Los hits de puntos de interrupción, excepciones y pausas siempre interrumpen
un paso de instrucción, así que un punto de interrupción establecido en una sub-línea de una expresión multi-línea aún
dispara como se espera.

## Tech Stack

- [textual](https://github.com/Textualize/textual) : Marco TUI
- [debugpy](https://github.com/microsoft/debugpy) : Implementación del Debug Adapter Protocol para Python
- [gdb](https://sourceware.org/gdb/) / [lldb-dap](https://lldb.llvm.org/resources/lldbdap.html) : adaptadores DAP opcionales, instalados por el usuario para C/C++
- [pygments](https://pygments.org/) : Resaltado de sintaxis
- [FastAPI](https://fastapi.tiangolo.com/) + [uvicorn](https://www.uvicorn.org/) : Servidor JSON-RPC

## Licencia

MIT


## Problemas Conocidos

Este comando
```
tdb --terminal gnome-terminal --python /path/to/venv/matplotlib/bin/python3 examples/double_pendulum.py
```
ignora los puntos de interrupción o falla después de mostrar el primer marco.
El argumento `--python` debe apuntar a una instalación con `matplotlib`.
