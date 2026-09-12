#include "main.h"
#include <stdio.h>

// 老版本主循环
int main(void)
{
    int counter = 0;
    while (1) {
        counter++;
        sleep_ms(10);
    }
    return 0;
}
