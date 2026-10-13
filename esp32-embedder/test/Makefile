# Host tests of the firmware's portable C: the batching planner and the segmented attention.
#   make          build and run both
CC ?= cc
CFLAGS ?= -O2 -std=gnu11 -Wall
FW = ../firmware/full/main

test: test_batch test_att
	./test_batch
	./test_att

test_batch: test_batch.c $(FW)/batch.c $(FW)/batch.h
	$(CC) $(CFLAGS) -Wextra -I$(FW) -o $@ test_batch.c $(FW)/batch.c

test_att: test_att.c att_old.inc stub/esp_host.h $(FW)/kernels.inc $(FW)/f16.h
	$(CC) $(CFLAGS) -Wno-unused-function -Istub -I$(FW) -o $@ test_att.c -lm

clean:
	rm -f test_batch test_att

.PHONY: test clean
