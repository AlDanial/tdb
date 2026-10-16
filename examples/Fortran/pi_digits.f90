! Build:  gfortran -g -O0 -fopenmp pi_digits.f90 -o pi_digits_f90
! Run:    ./pi_digits_f90 -r 2 -d 20 -t 2 -p
! Debug:  tdb -- ./pi_digits_f90 -r 2 -d 20 -t 2 -p
!
! Threads come from OpenMP directives; built without -fopenmp the program
! still works, computing each "thread" in turn.
module pi_mod
    implicit none
    integer, parameter :: i8 = selected_int_kind(18)
contains

    function seconds() result(now)
        real(kind=8) :: now
        integer(kind=i8) :: ticks, rate
        call system_clock(ticks, rate)
        now = real(ticks, kind=8) / real(rate, kind=8)
    end function seconds

    ! Bounded base-10 spigot. The first emitted character is a carry placeholder.
    recursive subroutine pi_digits(count, digits)
        integer, intent(in) :: count
        character, intent(out) :: digits(count)
        integer, allocatable :: remainders(:)
        character, allocatable :: out(:)
        integer :: size, used, pending, previous, step, i, k
        integer(kind=i8) :: quotient, value

        size = count * 10 / 3 + 1
        allocate(remainders(size), out(count + 3))
        remainders = 2
        used = 0
        pending = 0
        previous = 0
        do step = 0, count
            quotient = 0
            do i = size, 1, -1
                value = 10_i8 * remainders(i) + quotient * i
                remainders(i) = int(mod(value, int(2 * i - 1, i8)))
                quotient = value / (2 * i - 1)
            end do
            remainders(1) = int(mod(quotient, 10_i8))
            quotient = quotient / 10
            if (quotient == 9) then
                pending = pending + 1
            else if (quotient == 10) then
                used = used + 1
                out(used) = achar(iachar('0') + previous + 1)
                do k = 1, pending
                    used = used + 1
                    out(used) = '0'
                end do
                pending = 0
                previous = 0
            else
                used = used + 1
                out(used) = achar(iachar('0') + previous)
                do k = 1, pending
                    used = used + 1
                    out(used) = '9'
                end do
                pending = 0
                previous = int(quotient)
            end if
        end do
        used = used + 1
        out(used) = achar(iachar('0') + previous)
        do k = 1, pending
            used = used + 1
            out(used) = '9'
        end do
        digits = out(2:count + 1)
        deallocate(remainders, out)
    end subroutine pi_digits

    function positive(text) result(number)
        character(len=*), intent(in) :: text
        integer :: number, status
        status = 1
        if (len_trim(text) > 0 .and. len_trim(text) <= 6 .and. &
            verify(trim(text), '0123456789') == 0) then
            read(text, *, iostat=status) number
        end if
        if (status /= 0 .or. number < 1 .or. number > 100000) then
            write(0, '(2a)') 'expected a positive integer: ', trim(text)
            call exit(2)
        end if
    end function positive

    function fmt_seconds(t) result(text)
        real(kind=8), intent(in) :: t
        character(len=20) :: text
        write(text, '(f20.6)') t
        text = adjustl(text)
    end function fmt_seconds

end module pi_mod

program pi_digits_main
    use pi_mod
    implicit none
    integer :: runs, count, threads, run, j, i, nargs
    logical :: print_digits
    character(len=256) :: arg, prog
    character, allocatable :: digits(:, :)
    real(kind=8), allocatable :: elapsed(:)
    real(kind=8) :: run_start, start

    runs = 1
    count = 10
    threads = 1
    print_digits = .false.
    nargs = command_argument_count()
    call get_command_argument(0, prog)
    i = 1
    do while (i <= nargs)
        call get_command_argument(i, arg)
        if (arg == '-p' .or. arg == '--print') then
            print_digits = .true.
        else if (i < nargs .and. (arg == '-r' .or. arg == '--n-runs')) then
            i = i + 1
            call get_command_argument(i, arg)
            runs = positive(arg)
        else if (i < nargs .and. (arg == '-d' .or. arg == '--n-digits')) then
            i = i + 1
            call get_command_argument(i, arg)
            count = positive(arg)
        else if (i < nargs .and. (arg == '-t' .or. arg == '--n-threads')) then
            i = i + 1
            call get_command_argument(i, arg)
            threads = positive(arg)
        else
            write(0, '(3a)') 'usage: ', trim(prog), ' [-r runs] [-d digits] [-t threads] [-p]'
            call exit(2)
        end if
        i = i + 1
    end do

    do run = 1, runs
        run_start = seconds()
        allocate(digits(count, threads))
        allocate(elapsed(threads))
        !$omp parallel do num_threads(threads) schedule(static, 1) private(start)
        do j = 1, threads
            start = seconds()
            call pi_digits(count, digits(:, j))
            elapsed(j) = seconds() - start
        end do
        !$omp end parallel do
        do j = 1, threads
            if (threads > 1) then
                write(*, '(a,i3,a,i3,a,i3,a,i3,3a)') 'Run ', run, '/', runs, ', thread ', &
                    j, '/', threads, ' completed in ', trim(fmt_seconds(elapsed(j))), ' seconds.'
            else
                write(*, '(a,i3,a,i3,3a)') 'Run ', run, '/', runs, ' completed in ', &
                    trim(fmt_seconds(elapsed(j))), ' seconds.'
            end if
        end do
        if (threads > 1) then
            write(*, '(a,i3,a,i3,3a)') '== Run ', run, '/', runs, ' completed in ', &
                trim(fmt_seconds(seconds() - run_start)), ' seconds.'
        end if
        if (run == runs .and. print_digits) write(*, '(100000a)') digits(:, 1)
        deallocate(digits, elapsed)
    end do
end program pi_digits_main
