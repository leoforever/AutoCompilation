#ifndef SIMD_H
#define SIMD_H
#include <lsxintrin.h>
typedef union { int i32[4]; __m128i vr128; } loongarch_vector;
int lsx_add_check(void);
#endif
