# loongarch-simd-demo
# 缺陷链（层层递进，agent 需要都踩到/修掉才能成功）：
#   1. configure 架构表没有 loongarch64 → 直接报 Unsupported architecture
#   2. 就算加了条目，忘记给 SIMD 标记 → 代码里 __m128i/__m256i 未知类型
#   3. 补 -mlsx -mlasx 才能编译过并跑 PASS
include config.mk

CC      = gcc
CFLAGS  = -O2 -Wall

SRC     = src/simd.c src/lasx.c src/main.c
OBJ     = $(SRC:.c=.o)
BIN     = lsxdemo

all: $(BIN)

config.mk:
	./configure

$(BIN): config.mk $(OBJ)
	$(CC) $(CFLAGS) $(ARCH_CFLAGS) -o $@ $(OBJ)

%.o: %.c config.mk
	$(CC) $(CFLAGS) $(ARCH_CFLAGS) -c $< -o $@

clean:
	rm -f $(OBJ) $(BIN) config.mk

test: $(BIN)
	./$(BIN)
