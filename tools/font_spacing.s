.syntax unified
.cpu arm7tdmi
.thumb
.section .text
.global font_spacing
.thumb_func
font_spacing:
    ldr r2, [r2]
    ldr r1, [r1]
    ldr r3, small_font
    cmp r1, r3
    bne store_metrics
    ldr r3, small_size
    cmp r2, r3
    bne store_metrics
    adds r2, #1
store_metrics:
    str r2, [r0]
    str r1, [r0, #4]
    movs r0, #1
    bx lr
.balign 4
small_font:
    .word 0x08cf99f8
small_size:
    .word 0x000a000a
