/* 使用 LSX intrinsics —— 编译必须带 -mlsx，否则 "未知的类型名 __m128i" */
#include <lsxintrin.h>
#include "simd.h"

int lsx_add_check(void)
{
    int a[4] = {1, 2, 3, 4};
    int b[4] = {10, 20, 30, 40};
    int out[4];

    __m128i va = __lsx_vld((const loongarch_vector *)a, 0);
    __m128i vb = __lsx_vld((const loongarch_vector *)b, 0);
    __m128i vc = __lsx_vadd_w(va, vb);
    __lsx_vst(vc, (loongarch_vector *)out, 0);

    for (int i = 0; i < 4; i++) {
        if (out[i] != a[i] + b[i]) return 1;
    }
    return 0;
}
