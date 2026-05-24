#!/home/al/venvs/work/bin/python
import numpy as np
import sys
# from Intro to Applied Mathematics, G. Strang, p 39

data = np.asarray([[4.420062, -6409], [2.869763, 598], [1.470008, 6926]])

X = data[:, 0]
Y = data[:, 1]
m = len(X)

A11 = np.double(m)
A12 = sum(X)
A21 = A12
A22 = sum(X**2)

b1 = sum(Y)
b2 = sum(X * Y)

Determinant = A11 * A22 - A21 * A12
if abs(Determinant) < 1.0e-15:
    print("Infinite slope")
    sys.exit(1)

Inv11 = A22 / Determinant
Inv12 = -A12 / Determinant
Inv21 = -A21 / Determinant
Inv22 = A11 / Determinant

Offset = Inv11 * b1 + Inv12 * b2
Slope = Inv21 * b1 + Inv22 * b2

print("mine:  ", Slope, Offset)

(Gain, Offset) = np.polyfit(X, Y, 1)
print("scipy: ", Gain, Offset)
