/* C stub for Tdb.breakpoint: runs the shared tdb.h recipe with the
 * language id "ocaml". tdb.h here is a copy of tdb's packaged header
 * (src/tdb/adapters/native/tdb.h); a test keeps them identical. */
#define CAML_NAME_SPACE
#include <caml/mlvalues.h>
#include <caml/signals.h>

#include "tdb.h"

#if defined(__GNUC__) || defined(__clang__)
#define TDB_STUB_NOINLINE __attribute__((noinline))
#else
#define TDB_STUB_NOINLINE
#endif

TDB_STUB_NOINLINE value tdb_ocaml_breakpoint(value unit)
{
    (void)unit;
    /* Waiting for tdb to attach can take a while; do not hold the
     * runtime lock meanwhile (OCaml 5 domains keep running). The stop
     * itself freezes the whole process, lock or no lock. */
    caml_enter_blocking_section();
    tdb_breakpoint_lang("ocaml");
    caml_leave_blocking_section();
    return Val_unit;
}
