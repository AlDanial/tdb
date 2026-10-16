! Demo: drop into tdb at a specific line via tdb_breakpoint().
!
! Run it directly (not under tdb). tdb.h is a C header, so compile it as
! C into an object that supplies tdb_breakpoint(), then link that object
! with the Fortran program. Build unoptimized so locals stay inspectable,
! then run the binary from a terminal:
!
!   gcc -g -O0 -c -x c "$(tdb --info | awk -F': ' '/tdb.h dir/ {print $2}')/tdb.h" \
!       -o tdb.o
!   gfortran -g -O0 -o demo breakpoint_hook_demo.f90 tdb.o && ./demo
program breakpoint_hook_demo
    implicit none
    interface
        subroutine tdb_breakpoint() bind(C, name="tdb_breakpoint")
        end subroutine tdb_breakpoint
    end interface
    integer :: result

    result = compute(10)
    write(*, '(a,i0)') 'result = ', result

contains

    function compute(n) result(answer)
        integer, intent(in) :: n
        integer :: answer, total, i
        integer :: local_list(5)

        total = 0
        local_list = (/ 1, 2, 3, 4, 5 /)
        do i = 0, n - 1
            total = total + i
        end do
        call tdb_breakpoint()  ! tdb opens here; inspect total and local_list
        answer = total + local_list(5)
    end function compute

end program breakpoint_hook_demo
