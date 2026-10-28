/* 使用 LASX 256-bit intrinsics —— 编译必须带 -mlasx，否则 "未知的类型名 __m256i" */
#include "lasx.h"

int lasx_add_check(void)
{
    int a[8] = {1, 2, 3, 4, 5, 6, 7, 8};
    int b[8] = {10, 20, 30, 40, 50, 60, 70, 80};
    int out[8];

    __m256i va = __lasx_xvld((const loongarch_vector256 *)a, 0);
    __m256i vb = __lasx_xvld((const loongarch_vector256 *)b, 0);
    __m256i vc = __lasx_xvadd_w(va, vb);
    __lasx_xvst(vc, (loongarch_vector256 *)out, 0);

    for (int i = 0; i < 8; i++) {
        if (out[i] != a[i] + b[i]) return 1;
    }
    return 0;
}
