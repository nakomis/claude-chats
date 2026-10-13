// CHATS-4: ESP-IDF sets the S3's digital LDO for 240 MHz to the chip's factory pass point (eFuse
// DIG_DBIAS_HVT) + 3 steps, held to about 1.30 V (esp_hw_support/port/esp32s3/rtc_init.c). On some
// chips that margin isn't enough for both cores running PIE flat out: results come out wrong,
// a dot product at a time, differently each run. Three boards from one batch: 1.218 V and 1.228 V
// failed every long dual-core pass, 1.249 V was clean; raised to >= 1.25 V, all three are clean.
#include <stdio.h>

#include "esp_private/regi2c_ctrl.h"
#include "esp_rom_sys.h"
#include "hal/efuse_ll.h"
#include "soc/regi2c_dig_reg.h"
#include "soc/rtc.h"

#include "vcore.h"

// ESP-IDF's choice for 240 MHz: private globals of esp_hw_support (5.4.x; check them on an upgrade)
extern uint32_t g_dig_dbias_pvt_240m, g_rtc_dbias_pvt_240m;

// the calibration fields are sign-magnitude
static int sm(uint32_t v, int bits) { return v & (1u << (bits - 1)) ? -(int)(v & ((1u << (bits - 1)) - 1)) : (int)v; }

int vcore_boost(int min_mv)
{
    // Only with a valid calibration, exactly as ESP-IDF decides (rtc_set_stored_dbias): otherwise its
    // own defaults stand, and the voltages below would be computed from meaningless fields.
    uint8_t minor = efuse_ll_get_blk_version_minor(), major = efuse_ll_get_blk_version_major();
    if (!((major <= 1 && minor == 1) || major > 1 || (major == 1 && minor >= 2)) || efuse_ll_get_dig_dbias_hvt() == 0) {
        printf("vcore: no core-voltage calibration in eFuse; leaving ESP-IDF's defaults\n");
        return 0;
    }
    // volts x 10000 at a given dbias step, from this chip's eFuse calibration (as rtc_init.c)
    int kd = K_DIG_MID_MUL10000 + sm(efuse_ll_get_k_dig_ldo(), 7);
    int kr = K_RTC_MID_MUL10000 + sm(efuse_ll_get_k_rtc_ldo(), 7);
    int vd20 = V_DIG_MID_MUL10000 + sm(efuse_ll_get_v_dig_dbias20(), 8) * 10000 / 500;
    int vr20 = V_RTC_MID_MUL10000 + sm(efuse_ll_get_v_rtc_dbias20(), 8) * 10000 / 500;
#define VDIG(d) (vd20 + kd * ((int)(d) - 20))
#define VRTC(d) (vr20 + kr * ((int)(d) - 20))
    uint32_t d0 = g_dig_dbias_pvt_240m, r0 = g_rtc_dbias_pvt_240m, d = d0, r = 15;
    while (d < 31 && VDIG(d) < min_mv * 10 && VDIG(d + 1) <= 13000) d++;
    while (r < 31 && VRTC(r) < VDIG(d) - 250) r++;            // as ESP-IDF: rtc at most 25 mV below dig
    if (r < r0) r = r0;
    // On some chips rtc can't follow even ESP-IDF's own choice (one board here: rtc already at 31,
    // 42 mV below dig at stock). Backing dig off to keep 25 mV would undo the fix on exactly such a
    // chip, so keep it and say so.
    if (VRTC(r) < VDIG(d) - 250)
        printf("vcore: rtc rail tops out %.0f mV below the core (ESP-IDF aims for <= 25 mV)\n", (VDIG(d) - VRTC(r)) / 10.0);
    printf("vcore: 240 MHz core %.3f V -> %.3f V (dbias %lu -> %lu, rtc %lu -> %lu)\n", VDIG(d0) / 1e4, VDIG(d) / 1e4,
           (unsigned long)d0, (unsigned long)d, (unsigned long)r0, (unsigned long)r);
    if (d != d0 || r != r0) {
        REGI2C_WRITE_MASK(I2C_DIG_REG, I2C_DIG_REG_EXT_RTC_DREG, r);   // raise: rtc first, as ESP-IDF does
        REGI2C_WRITE_MASK(I2C_DIG_REG, I2C_DIG_REG_EXT_DIG_DREG, d);
        esp_rom_delay_us(40);
        g_dig_dbias_pvt_240m = d;                             // and any later switch back to 240 MHz
        g_rtc_dbias_pvt_240m = r;
    }
    return VDIG(d) / 10;
}
