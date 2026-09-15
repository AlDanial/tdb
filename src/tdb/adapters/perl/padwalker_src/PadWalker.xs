#define PERL_NO_GET_CONTEXT
#include "EXTERN.h"
#include "perl.h"
#include "XSUB.h"

#ifndef isGV_with_GP
#define isGV_with_GP(x) isGV(x)
#endif

#ifndef CxOLD_OP_TYPE
#  define CxOLD_OP_TYPE(cx)      (0 + (cx)->blk_eval.old_op_type)
#endif

#ifndef CvISXSUB
#define CvISXSUB(sv) CvXSUB(sv)
#endif

#ifndef PadnameUTF8
#  define PadnameUTF8(pn) FALSE
#endif

/* For development testing */
#ifdef PADWALKER_DEBUGGING
# define debug_print(x) printf x
#else
# define debug_print(x)
#endif

/* For debugging */
#ifdef PADWALKER_DEBUGGING
char *
cxtype_name(U32 cx_type)
{
  switch(cx_type & CXTYPEMASK)
  {
    case CXt_NULL:   return "null";
    case CXt_SUB:    return "sub";
    case CXt_EVAL:   return "eval";
    case CXt_LOOP:   return "loop";
    case CXt_SUBST:  return "subst";
    case CXt_BLOCK:  return "block";
    case CXt_FORMAT: return "format";

    default:         debug_print(("Unknown context type 0x%lx\n", cx_type));
                                         return "(unknown)";
  }
}

void
show_cxstack(void)
{
    I32 i;
    for (i = cxstack_ix; i>=0; --i)
    {
        printf(" =%ld= %s (%lx)", (long)i,
            cxtype_name(CxTYPE(&cxstack[i])), cxstack[i].blk_oldcop->cop_seq);
        if (CxTYPE(&cxstack[i]) == CXt_SUB) {
              CV *cv = cxstack[i].blk_sub.cv;
              printf("\t%s", (cv && CvGV(cv)) ? GvNAME(CvGV(cv)) :"(null)");
        }
        printf("\n");
    }
}
#else
# define show_cxstack()
#endif

#ifndef SvOURSTASH
# ifdef OURSTASH
#  define SvOURSTASH OURSTASH
# else
#  define SvOURSTASH GvSTASH
# endif
#endif

#ifndef COP_SEQ_RANGE_LOW
#  define COP_SEQ_RANGE_LOW(sv)                  U_32(SvNVX(sv))
#endif
#ifndef COP_SEQ_RANGE_HIGH
#  define COP_SEQ_RANGE_HIGH(sv)                 U_32(SvUVX(sv))
#endif

#ifndef PadARRAY
typedef AV PADNAMELIST;
typedef SV PADNAME;
# if PERL_VERSION < 8 || (PERL_VERSION == 8 && !PERL_SUBVERSION)
typedef AV PADLIST;
typedef AV PAD;
# endif
# define PadlistARRAY(pl)	((PAD **)AvARRAY(pl))
# define PadlistMAX(pl)		AvFILLp(pl)
# define PadlistNAMES(pl)	(*PadlistARRAY(pl))
# define PadnamelistARRAY(pnl)	((PADNAME **)AvARRAY(pnl))
# define PadnamelistMAX(pnl)	AvFILLp(pnl)
# define PadARRAY		AvARRAY
# define PadnameIsOUR(pn)	!!(SvFLAGS(pn) & SVpad_OUR)
# define PadnameOURSTASH(pn)	SvOURSTASH(pn)
# define PadnameOUTER(pn)	!!SvFAKE(pn)
# define PadnamePV(pn)		(SvPOKp(pn) ? SvPVX(pn) : NULL)
#endif


/* Originally stolen from pp_ctl.c; now significantly different */

I32
dopoptosub_at(pTHX_ PERL_CONTEXT *cxstk, I32 startingblock)
{
    dTHR;
    I32 i;
    PERL_CONTEXT *cx;
    for (i = startingblock; i >= 0; i--) {
        cx = &cxstk[i];
        switch (CxTYPE(cx)) {
        default:
            continue;
        case CXt_SUB:
        /* In Perl 5.005, formats just used CXt_SUB */
#ifdef CXt_FORMAT
       case CXt_FORMAT:
#endif
            debug_print(("**dopoptosub_at: found sub #%ld\n", (long)i));
            return i;
        }
    }
        debug_print(("**dopoptosub_at: not found #%ld\n", (long)i));
    return i;
}

I32
dopoptosub(pTHX_ I32 startingblock)
{
    dTHR;
    return dopoptosub_at(aTHX_ cxstack, startingblock);
}

/* This function is based on the code of pp_caller */
PERL_CONTEXT*
upcontext(pTHX_ I32 count, COP **cop_p, PERL_CONTEXT **ccstack_p,
                                I32 *cxix_from_p, I32 *cxix_to_p)
{
    PERL_SI *top_si = PL_curstackinfo;
    I32 cxix = dopoptosub(aTHX_ cxstack_ix);
    PERL_CONTEXT *ccstack = cxstack;

    if (cxix_from_p) *cxix_from_p = cxstack_ix+1;
    if (cxix_to_p)   *cxix_to_p   = cxix;
    for (;;) {
        /* we may be in a higher stacklevel, so dig down deeper */
        while (cxix < 0 && top_si->si_type != PERLSI_MAIN) {
            top_si  = top_si->si_prev;
            ccstack = top_si->si_cxstack;
            cxix = dopoptosub_at(aTHX_ ccstack, top_si->si_cxix);
                        if (cxix_to_p && cxix_from_p) *cxix_from_p = *cxix_to_p;
                        if (cxix_to_p) *cxix_to_p = cxix;
        }
        if (cxix < 0 && count == 0) {
                    if (ccstack_p) *ccstack_p = ccstack;
            return (PERL_CONTEXT *)0;
                }
        else if (cxix < 0)
            return (PERL_CONTEXT *)-1;
        if (PL_DBsub && cxix >= 0 &&
                ccstack[cxix].blk_sub.cv == GvCV(PL_DBsub))
            count++;
        if (!count--)
            break;

        if (cop_p) *cop_p = ccstack[cxix].blk_oldcop;
        cxix = dopoptosub_at(aTHX_ ccstack, cxix - 1);
                        if (cxix_to_p && cxix_from_p) *cxix_from_p = *cxix_to_p;
                        if (cxix_to_p) *cxix_to_p = cxix;
    }
    if (ccstack_p) *ccstack_p = ccstack;
    return &ccstack[cxix];
}

/* end thievery */

SV*
fetch_from_stash(pTHX_ HV *stash, char *name_str, U32 name_len)
{
    /* This isn't the most efficient approach, but it has
     * the advantage that it uses documented API functions. */
    char *package_name = HvNAME(stash);
    char *qualified_name;
    SV *ret = 0;  /* Initialise to silence spurious compiler warning */
    
    New(0, qualified_name, strlen(package_name) + 2 + name_len, char);
    strcpy(qualified_name, package_name);
    strcat(qualified_name, "::");
    strcat(qualified_name, name_str+1);

    debug_print(("fetch_from_stash: Looking for %c%s\n",
                 name_str[0], qualified_name));
    switch (name_str[0]) {
      case '$': ret =       get_sv(qualified_name, FALSE); break;
      case '@': ret = (SV*) get_av(qualified_name, FALSE); break;
      case '%': ret = (SV*) get_hv(qualified_name, FALSE); break;
      default:  die("PadWalker: variable '%s' of unknown type", name_str);
    }
    if (ret)
      debug_print(("%s\n", sv_peek(ret)));
    else
      /* I don't _think_ this should ever happen */
      debug_print(("XXXX - Variable %c%s not found\n",
                   name_str[0], qualified_name));
    Safefree(qualified_name);
    return ret;
}

void
pads_into_hash(pTHX_ PADNAMELIST* pad_namelist, PAD* pad_vallist, HV* my_hash,
               HV* our_hash, U32 valid_at_seq)
{
    I32 i;

    debug_print(("pads_into_hash(%p, %p, ..)\n",
        (void*)pad_namelist, (void*) pad_vallist));

    for (i=PadnamelistMAX(pad_namelist); i>=0; --i) {
      PADNAME* name_sv = PadnamelistARRAY(pad_namelist)[i];

      if (name_sv) {
        char *name_str = PadnamePV(name_sv);
        if (name_str) {

        debug_print(("** %s (%lx,%lx) [%lx]%s\n", name_str,
               COP_SEQ_RANGE_LOW(name_sv), COP_SEQ_RANGE_HIGH(name_sv), valid_at_seq,
               PadnameOUTER(name_sv) ? " <fake>" : ""));
        
        /* Check that this variable is valid at the cop_seq
         * specified, by peeking into the NV and IV slots
         * of the name sv. (This must be one of those "breathtaking
         * optimisations" mentioned in the Panther book).

         * Anonymous subs are stored here with a name of "&",
         * so also check that the name is longer than one char.
         * (Note that the prefix letter is here as well, so a
         * valid variable will _always_ be >1 char)
         */

        if ((PadnameOUTER(name_sv) || 0 == valid_at_seq ||
            (valid_at_seq <= COP_SEQ_RANGE_HIGH(name_sv) &&
            valid_at_seq > COP_SEQ_RANGE_LOW(name_sv))) &&
            strlen(name_str) > 1 )

          {
            SV *val_sv;
            U32 name_len = strlen(name_str);
            bool is_our = PadnameIsOUR(name_sv);

            debug_print(((is_our ? "**     FOUND OUR %s\n"
                                 : "**     FOUND MY %s\n"), name_str));

            if (   hv_exists(my_hash, name_str, name_len)
                || hv_exists(our_hash, name_str, name_len))
            {
              debug_print(("** key already exists - ignoring!\n"));
            }
            else {
              if (is_our) {
                val_sv = fetch_from_stash(aTHX_ PadnameOURSTASH(name_sv),
                                          name_str, name_len);
                if (!val_sv) {
                    debug_print(("Value of our variable is undefined\n"));
                    val_sv = &PL_sv_undef;
                }
              }
              else
              {
                val_sv =
                  pad_vallist ? PadARRAY(pad_vallist)[i] : &PL_sv_undef;
                if (!val_sv) val_sv = &PL_sv_undef;
              }

              hv_store((is_our ? our_hash : my_hash), name_str, PadnameUTF8(name_sv) ? -name_len : name_len,
                       (val_sv ? newRV_inc(val_sv) : &PL_sv_undef), 0);
            }
          }
        }
      }
    }
}

void
padlist_into_hash(pTHX_ PADLIST* padlist, HV* my_hash, HV* our_hash,
                  U32 valid_at_seq, long depth)
{
    PADNAMELIST *pad_namelist;
    PAD *pad_vallist;
    
    if (depth == 0) depth = 1;

    if (!padlist) {
        /* Probably an XSUB */
        die("PadWalker: cv has no padlist");
    }
    pad_namelist = PadlistNAMES(padlist);
    pad_vallist  = PadlistARRAY(padlist)[depth];

    pads_into_hash(aTHX_ pad_namelist, pad_vallist, my_hash, our_hash, valid_at_seq);
}

void
context_vars(pTHX_ PERL_CONTEXT *cx, HV* my_ret, HV* our_ret, U32 seq, CV *cv)
{
    /* If cx is null, we take that to mean that we should look
     * at the cv instead
     */

    debug_print(("**context_vars(%p, %p, %p, 0x%lx)\n",
                 (void*)cx, (void*)my_ret, (void*)our_ret, (long)seq));
    if (cx == (PERL_CONTEXT*)-1)
        croak("Not nested deeply enough");

    else {
        CV*  cur_cv = cx ? cx->blk_sub.cv           : cv;
        long depth  = cx ? cx->blk_sub.olddepth + 1 : 1;

        if (!cur_cv)
            die("panic: Context has no CV!\n");
    
        while (cur_cv) {
            debug_print(("\tcv name = %s; depth=%ld\n",
                    CvGV(cur_cv) ? GvNAME(CvGV(cur_cv)) :"(null)", depth));
            if (CvPADLIST(cur_cv))
                padlist_into_hash(aTHX_ CvPADLIST(cur_cv), my_ret, our_ret, seq, depth);
            cur_cv = CvOUTSIDE(cur_cv);
            if (cur_cv) depth  = CvDEPTH(cur_cv);
        }
    }
}

void
do_peek(pTHX_ I32 uplevel, HV* my_hash, HV* our_hash)
{
    PERL_CONTEXT *cx, *ccstack;
    COP *cop = 0;
    I32 cxix_from, cxix_to, i;
    bool first_eval = TRUE;

    show_cxstack();
    if (PL_curstackinfo->si_type != PERLSI_MAIN)
          debug_print(("!! We're in a higher stack level\n"));

    cx = upcontext(aTHX_ uplevel, &cop, &ccstack, &cxix_from, &cxix_to);
    debug_print(("** cxix = (%ld,%ld)\n", cxix_from, cxix_to));
    if (cop == 0) {
           debug_print(("**Setting cop to PL_curcop\n"));
           cop = PL_curcop;
        }
    debug_print(("**Cop file = %s\n", CopFILE(cop)));

    context_vars(aTHX_ cx, my_hash, our_hash, cop->cop_seq, PL_main_cv);

    for (i = cxix_from-1; i > cxix_to; --i) {
        debug_print(("** CxTYPE = %s (cxix = %ld)\n",
            cxtype_name(CxTYPE(&ccstack[i])), i));
        switch (CxTYPE(&ccstack[i])) {
        case CXt_EVAL:
            debug_print(("\told_op_type = %ld\n", CxOLD_OP_TYPE(&ccstack[i])));
            switch(CxOLD_OP_TYPE(&ccstack[i])) {
            case OP_ENTEREVAL:
                if (first_eval) {
                   context_vars(aTHX_ 0, my_hash, our_hash, cop->cop_seq, ccstack[i].blk_eval.cv);
                   first_eval = FALSE;
                }
                context_vars(aTHX_ 0, my_hash, our_hash, ccstack[i].blk_oldcop->cop_seq,
                                                ccstack[i].blk_eval.cv);
                break;
            case OP_REQUIRE:
            case OP_DOFILE:
                debug_print(("blk_eval.cv = %p\n", (void*) ccstack[i].blk_eval.cv));
                if (first_eval)
                   context_vars(aTHX_ 0, my_hash, our_hash,
                    cop->cop_seq, ccstack[i].blk_eval.cv);
                return;
                /* If it's OP_ENTERTRY, we skip this altogether. */
            }
            break;

        case CXt_SUB:
#ifdef CXt_FORMAT
        case CXt_FORMAT:
#endif
                Perl_die(aTHX_ "PadWalker: internal error");
                    exit(EXIT_FAILURE);
        }
    }
}

void
get_closed_over(pTHX_ CV *cv, HV *hash, HV *indices)
{
    I32 i;
    U32 val_depth;
    PADNAMELIST *pad_namelist;
    PAD *pad_vallist;

    if (CvISXSUB(cv) || !CvPADLIST(cv)) {
        return;
    }

    val_depth = CvDEPTH(cv) ? CvDEPTH(cv) : 1;
    pad_namelist = PadlistNAMES(CvPADLIST(cv));
    pad_vallist  = PadlistARRAY(CvPADLIST(cv))[val_depth];

    debug_print(("PadlistMAX(CvPADLIST(cv)) = %ld\n",
                  PadlistMAX(CvPADLIST(cv)) ));
    
    for (i=PadnamelistMAX(pad_namelist); i>=0; --i) {
      PADNAME* name_sv = PadnamelistARRAY(pad_namelist)[i];

      if (name_sv && PadnamePV(name_sv)) {
        char* name_str  = PadnamePV(name_sv);
        STRLEN name_len = strlen(name_str);
        
        if (PadnameOUTER(name_sv) && !PadnameIsOUR(name_sv)) {
            SV *val_sv   = PadARRAY(pad_vallist)[i];
            if (!val_sv) val_sv = &PL_sv_undef;
#ifdef PADWALKER_DEBUGGING
            debug_print(("Found a fake slot: %s\n", name_str));
            if (val == 0)
                debug_print(("value is null\n"));
            else
                sv_dump(*val);
#endif
            hv_store(hash, name_str, name_len, newRV_inc(val_sv), 0);
            if (indices) {
              /* Create a temporary SV as a way of getting perl to 
               * stringify 'i' for us. */
              SV *i_sv = newSViv(i);
              hv_store_ent(indices, i_sv, newRV_inc(val_sv), 0);
              SvREFCNT_dec(i_sv);
            }
        }
      }
    }
}

char *
get_var_name(CV *cv, SV *var)
{
    I32 i;
    U32 val_depth = CvDEPTH(cv) ? CvDEPTH(cv) : 1;
    PADNAMELIST *pad_namelist = PadlistNAMES(CvPADLIST(cv));
    PAD *pad_vallist  = PadlistARRAY(CvPADLIST(cv))[val_depth];

    for (i=PadnamelistMAX(pad_namelist); i>=0; --i) {
      PADNAME* name = PadnamelistARRAY(pad_namelist)[i];
      char* name_str;

      if (  name && (name_str = PadnamePV(name))
         && PadARRAY(pad_vallist)[i] == var) {
          return name_str;
      }
    }
    return 0;
}

CV *
up_cv(pTHX_ I32 uplevel, const char * caller_name)
{
    PERL_CONTEXT *cx, *ccstack;
    I32 cxix_from, cxix_to, i;

    if (uplevel < 0)
      croak("%s: sub is < 0", caller_name);

    cx = upcontext(aTHX_ uplevel, 0, &ccstack, &cxix_from, &cxix_to);
    if (cx == (PERL_CONTEXT *)-1) {
      croak("%s: Not nested deeply enough", caller_name);
      return 0;  /* NOT REACHED, but stop picky compilers from whining */
    }
    else if (cx)
      return cx->blk_sub.cv;
      
    else {

      for (i = cxix_from-1; i > cxix_to; --i)
        if (CxTYPE(&ccstack[i]) == CXt_EVAL) {
          I32 old_op_type = CxOLD_OP_TYPE(&ccstack[i]);
          if (old_op_type == OP_REQUIRE || old_op_type == OP_DOFILE)
            return ccstack[i].blk_eval.cv;
        }

      return PL_main_cv;
    }
}

STATIC bool
is_scalar_type(SV *sv) {
    return !(
        SvTYPE(sv) == SVt_PVAV
     || SvTYPE(sv) == SVt_PVHV
     || SvTYPE(sv) == SVt_PVCV
     || isGV_with_GP(sv)
     || SvTYPE(sv) == SVt_PVIO
   );
}

STATIC bool
is_correct_type(SV *orig, SV *restore) {
    return (
        ( SvTYPE(orig) == SvTYPE(restore) )
            ||
        ( is_scalar_type(orig) && is_scalar_type(restore) )
    );
}


MODULE = PadWalker              PACKAGE = PadWalker
PROTOTYPES: DISABLE             

void
peek_my(uplevel)
I32 uplevel;
 PREINIT:
    HV* ret = newHV();
    HV* ignore = newHV();
 PPCODE:
    do_peek(aTHX_ uplevel, ret, ignore);
    SvREFCNT_dec((SV*) ignore);
    EXTEND(SP, 1);
    PUSHs(sv_2mortal(newRV_noinc((SV*)ret)));

void
peek_our(uplevel)
I32 uplevel;
 PREINIT:
    HV* ret = newHV();
    HV* ignore = newHV();
 PPCODE:
    do_peek(aTHX_ uplevel, ignore, ret);
    SvREFCNT_dec((SV*) ignore);
    EXTEND(SP, 1);
    PUSHs(sv_2mortal(newRV_noinc((SV*)ret)));


void
peek_sub(cv)
CV* cv;
  PREINIT:
    HV* ret = newHV();
    HV* ignore = newHV();
  PPCODE:
    if (CvISXSUB(cv))
      die("PadWalker: cv has no padlist");
    padlist_into_hash(aTHX_ CvPADLIST(cv), ret, ignore, 0, CvDEPTH(cv));
    SvREFCNT_dec((SV*) ignore);
    EXTEND(SP, 1);
    PUSHs(sv_2mortal(newRV_noinc((SV*)ret)));

void
set_closed_over(sv, pad)
SV* sv;
HV* pad;
  PREINIT:
    I32 i;
    CV *cv = (CV *)SvRV(sv);
    U32 val_depth = CvDEPTH(cv) ? CvDEPTH(cv) : 1;
    PADNAMELIST *pad_namelist = PadlistNAMES(CvPADLIST(cv));
    PAD *pad_vallist  = PadlistARRAY(CvPADLIST(cv))[val_depth];
  CODE:
    for (i=PadnamelistMAX(pad_namelist); i>=0; --i) {
      PADNAME* name = PadnamelistARRAY(pad_namelist)[i];
      char* name_str;

      if (name && (name_str = PadnamePV(name))) {
        STRLEN name_len = strlen(name_str);

        if (PadnameOUTER(name) && !PadnameIsOUR(name)) {
          SV **restore_ref = hv_fetch(pad, name_str, name_len, FALSE);
          if ( restore_ref ) {
            if ( SvROK(*restore_ref) ) {
              SV *restore = SvRV(*restore_ref);
              SV *orig = PadARRAY(pad_vallist)[i];
              int restore_type = SvTYPE(restore);

              if ( !orig || is_correct_type(orig, restore) ) {
                SvREFCNT_inc(restore);

                PadARRAY(pad_vallist)[i] = restore;
              } else {
                croak("Incorrect reftype for variable %s (got %s expected %s)", name_str, sv_reftype(restore, 0), sv_reftype(orig, 0));
              }
            } else {
              croak("The variable for %s is not a reference", name_str);
            }
          }
        }
      }
    }



void
closed_over(cv)
CV* cv;
  PREINIT:
    HV* ret = newHV();
    HV* targs;
  PPCODE:
    if (GIMME_V == G_ARRAY) {
        targs = newHV();
        get_closed_over(aTHX_ cv, ret, targs);
    
        EXTEND(SP, 2);
        PUSHs(sv_2mortal(newRV_noinc((SV*)ret)));
        PUSHs(sv_2mortal(newRV_noinc((SV*)targs)));
    }
    else {
        get_closed_over(aTHX_ cv, ret, 0);
        
        EXTEND(SP, 1);
        PUSHs(sv_2mortal(newRV_noinc((SV*)ret)));
    }

char*
var_name(sub, var_ref)
SV* sub;
SV* var_ref;
  PREINIT:
    SV *cv;
  CODE:
    if (!SvROK(var_ref))
      croak("Usage: PadWalker::var_name(sub, var_ref)");
      
    if (SvROK(sub)) {
      cv = SvRV(sub);
      if (SvTYPE(cv) != SVt_PVCV)
        croak("PadWalker::var_name: sub is neither a CODE reference nor a number");
    } else
      cv = (SV *) up_cv(aTHX_ SvIV(sub), "PadWalker::upcontext");
    
    RETVAL = get_var_name((CV *) cv, SvRV(var_ref));
  OUTPUT:
    RETVAL

void
_upcontext(uplevel)
I32 uplevel
  PPCODE:
    /* This is used by Devel::Caller. */
    XPUSHs(sv_2mortal(newSViv((IV)upcontext(aTHX_ uplevel, 0, 0, 0, 0))));
