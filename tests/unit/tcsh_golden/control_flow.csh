# leading comment
set message = "quoted # remains text"
if ($flag) then
foreach item (one two)
switch ($item)
case one:
echo "one" ; echo "still one"
breaksw
default:
echo default
endsw
end
endif
again:
set values = (one \
two)
source "lib/helper.csh"
source "$where/file.csh"
