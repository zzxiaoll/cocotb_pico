/*
This is free and unencumbered software released into the public domain.

Anyone is free to copy, modify, publish, use, compile, sell, or
distribute this software, either in source code form or as a compiled
binary, for any purpose, commercial or non-commercial, and by any
means.
*/

ENTRY(reset_vec)

MEMORY {
	/* Keep the program above Spike's low-address debug region. The firmware
	 * stack starts at 0x20000 and grows downwards. */
	mem : ORIGIN = 0x00010000, LENGTH = 0x00010000
}

SECTIONS {
	.memory : {
		start*(.text);
		*(.text);
		*(*);
		end = .;
		. = ALIGN(4);
	} > mem
}
