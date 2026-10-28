#include <stdio.h>
#include "simd.h"
#include "lasx.h"

int main(void)
{
    int rc = 0;
    if (lsx_add_check() == 0)
        printf("LSX add check: PASS\n");
    else { printf("LSX add check: FAIL\n"); rc = 1; }
    if (lasx_add_check() == 0)
        printf("LASX add check: PASS\n");
    else { printf("LASX add check: FAIL\n"); rc = 1; }
    return rc;
}
