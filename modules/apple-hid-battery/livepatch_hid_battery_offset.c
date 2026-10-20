// SPDX-License-Identifier: GPL-2.0
/*
 * Livepatch backport of upstream commit d07644524b65 ("HID: input: read
 * battery capacity from its actual report offset") for Ubuntu 7.0 kernels.
 *
 * hidinput_query_battery_capacity() reads the capacity from buf[1], but Apple
 * Magic Keyboard / Magic Trackpad 2 battery reports (id 0x90) carry a flags
 * byte first and the AbsoluteStateOfCharge byte second, so userspace sees the
 * flags (0%..5%) instead of the charge. This replaces only that function and
 * reads the byte at the battery field's real offset in the report.
 *
 * Kernels <= 7.0 keep a single battery per device in struct hid_device, so the
 * battery field is located from the report instead of a stored offset (which
 * would change struct hid_device and break the ABI of every HID module).
 */
#define pr_fmt(fmt) KBUILD_MODNAME ": " fmt

#include <linux/hid.h>
#include <linux/kernel.h>
#include <linux/livepatch.h>
#include <linux/module.h>
#include <linux/slab.h>

/* Usages for which hidinput_setup_battery() registers the battery. */
static bool is_battery_usage(unsigned int hid)
{
	return hid == (HID_UP_DIGITIZER | 0x3b) ||
	       hid == HID_DC_BATTERYSTRENGTH ||
	       hid == HID_BAT_ABSOLUTESTATEOFCHARGE;
}

/* Byte index of the battery value in a raw report (byte 0 is the report id). */
static unsigned int battery_byte_offset(struct hid_device *dev)
{
	struct hid_report *report;
	unsigned int i, j;

	report = dev->report_enum[dev->battery_report_type]
			 .report_id_hash[dev->battery_report_id];
	if (!report)
		return 1;

	for (i = 0; i < report->maxfield; i++) {
		struct hid_field *field = report->field[i];

		for (j = 0; j < field->maxusage; j++)
			if (is_battery_usage(field->usage[j].hid))
				return 1 + field->report_offset / 8;
	}
	return 1;
}

static int livepatch_hidinput_query_battery_capacity(struct hid_device *dev)
{
	unsigned int offset = battery_byte_offset(dev);
	int len = offset + 1;
	int value, ret;
	u8 *buf;

	buf = kmalloc(max(len, 4), GFP_KERNEL);
	if (!buf)
		return -ENOMEM;

	ret = hid_hw_raw_request(dev, dev->battery_report_id, buf, max(len, 4),
				 dev->battery_report_type, HID_REQ_GET_REPORT);
	if (ret < len) {
		kfree(buf);
		return -ENODATA;
	}

	value = buf[offset];
	kfree(buf);

	if (dev->battery_min < dev->battery_max &&
	    value >= dev->battery_min && value <= dev->battery_max)
		value = ((value - dev->battery_min) * 100) /
			(dev->battery_max - dev->battery_min);

	return value;
}

static struct klp_func funcs[] = {
	{
		.old_name = "hidinput_query_battery_capacity",
		.new_func = livepatch_hidinput_query_battery_capacity,
	},
	{ }
};

static struct klp_object objs[] = {
	{
		.name = "hid",
		.funcs = funcs,
	},
	{ }
};

static struct klp_patch patch = {
	.mod = THIS_MODULE,
	.objs = objs,
};

static int __init livepatch_init(void)
{
	return klp_enable_patch(&patch);
}

static void __exit livepatch_exit(void)
{
}

module_init(livepatch_init);
module_exit(livepatch_exit);
MODULE_DESCRIPTION("Backport: read HID battery capacity from its report offset");
MODULE_LICENSE("GPL");
MODULE_INFO(livepatch, "Y");
