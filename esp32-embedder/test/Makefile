# Host tests of the firmware's portable C (CHATS-5): the worker's modules (../worker) and the
# segmented attention (../firmware/full/main/kernels.inc).
#   make            build and run them all
#   make test_age   one of them
# mbedTLS and cJSON come from ESP-IDF's own tree, so the host tests run the same library code as
# the board: IDF_PATH (default ~/esp/esp-idf), as in CI's espressif/idf:v5.4.4 container.
IDF_PATH ?= $(HOME)/esp/esp-idf
MBEDTLS := $(IDF_PATH)/components/mbedtls/mbedtls
CJSON := $(IDF_PATH)/components/json/cJSON
CC ?= cc
B := build
W := ../worker
FW := ../firmware/full/main
SAN ?= -fsanitize=address,undefined -fno-omit-frame-pointer
CFLAGS ?= -O1 -g -std=gnu11 -Wall -Wextra -Werror $(SAN)
INC := -I$(W) -I$(MBEDTLS)/include -I$(CJSON)
LDLIBS := -lm

TESTS := test_kw test_sigv4 test_age test_prep test_result test_sqs test_creds test_worker test_tag test_batch test_att

all: $(addprefix $(B)/,$(TESTS)) $(B)/testkit
	cd $(B) && for t in $(TESTS); do echo "== $$t"; ./$$t || exit 1; done

$(B):
	mkdir -p $@

# mbedTLS's crypto (no TLS or X.509), built once, without the sanitisers or -Werror.
MBED_SRCS := $(filter-out ssl% x509% pkcs7% net_sockets% debug% mps%,$(notdir $(wildcard $(MBEDTLS)/library/*.c)))
MBED_OBJS := $(addprefix $(B)/mbedtls/,$(MBED_SRCS:.c=.o))
$(B)/mbedtls/%.o: $(MBEDTLS)/library/%.c | $(B)
	@mkdir -p $(B)/mbedtls
	@$(CC) -O2 -std=gnu11 -I$(MBEDTLS)/include -I$(MBEDTLS)/library -c $< -o $@
$(B)/libmbedcrypto.a: $(MBED_OBJS)
	ar rcs $@ $^
$(B)/cjson.o: $(CJSON)/cJSON.c | $(B)
	$(CC) -O2 -std=gnu11 -c $< -o $@

WORKER := $(wildcard $(W)/*.c)
HDRS := $(wildcard $(W)/*.h) testutil.h
LIBS := $(B)/libmbedcrypto.a $(B)/cjson.o

$(B)/test_%: test_%.c $(WORKER) $(HDRS) $(LIBS) ../tok/tok.h | $(B)
	$(CC) $(CFLAGS) $(INC) -I../tok -o $@ $< $(WORKER) $(LIBS) $(LDLIBS)

# the selftest.h tag pin (embedder tag vs CRC table)
$(B)/test_tag: test_tag.c $(FW)/selftest.h | $(B)
	$(CC) $(CFLAGS) -I$(FW) -o $@ $<

# from draft PR #21: the planner, and the segmented attention compiled off the chip
$(B)/test_batch: test_batch.c $(W)/batch.c $(W)/batch.h | $(B)
	$(CC) $(CFLAGS) -I$(W) -o $@ test_batch.c $(W)/batch.c
$(B)/test_att: test_att.c att_old.inc stub/esp_host.h $(FW)/kernels.inc $(FW)/f16.h | $(B)
	$(CC) -O2 -std=gnu11 -Wall -Wno-unused-function -Istub -I$(FW) -o $@ test_att.c -lm

# age's test kit (C2SP CCTV): decompressed and split up by testkit.py, then run by test_age
$(B)/testkit: age-testkit/* testkit.py | $(B)
	rm -rf $@ && python3 -I testkit.py age-testkit $@

clean:
	rm -rf $(B)

.PHONY: all clean
