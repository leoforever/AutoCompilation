#ifndef LASX_H
#define LASX_H
#include <lasxintrin.h>
typedef union { int i32[8]; __m256i vr256; } loongarch_vector256;
int lasx_add_check(void);
#endif
