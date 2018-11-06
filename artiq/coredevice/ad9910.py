from numpy import int32, int64

from artiq.language.core import (
    kernel, delay, portable, delay_mu, now_mu, at_mu)
from artiq.language.units import us, ms

from artiq.coredevice import spi2 as spi
from artiq.coredevice import urukul
# Work around ARTIQ-Python import machinery
urukul_sta_pll_lock = urukul.urukul_sta_pll_lock
urukul_sta_smp_err = urukul.urukul_sta_smp_err


__all__ = [
    "AD9910",
    "PHASE_MODE_CONTINUOUS", "PHASE_MODE_ABSOLUTE", "PHASE_MODE_TRACKING"
]


_PHASE_MODE_DEFAULT = -1
PHASE_MODE_CONTINUOUS = 0
PHASE_MODE_ABSOLUTE = 1
PHASE_MODE_TRACKING = 2

_AD9910_REG_CFR1 = 0x00
_AD9910_REG_CFR2 = 0x01
_AD9910_REG_CFR3 = 0x02
_AD9910_REG_AUX_DAC = 0x03
_AD9910_REG_IO_UPD = 0x04
_AD9910_REG_FTW = 0x07
_AD9910_REG_POW = 0x08
_AD9910_REG_ASF = 0x09
_AD9910_REG_MSYNC = 0x0A
_AD9910_REG_DRAMPL = 0x0B
_AD9910_REG_DRAMPS = 0x0C
_AD9910_REG_DRAMPR = 0x0D
_AD9910_REG_PR0 = 0x0E
_AD9910_REG_PR1 = 0x0F
_AD9910_REG_PR2 = 0x10
_AD9910_REG_PR3 = 0x11
_AD9910_REG_PR4 = 0x12
_AD9910_REG_PR5 = 0x13
_AD9910_REG_PR6 = 0x14
_AD9910_REG_PR7 = 0x15
_AD9910_REG_RAM = 0x16


class AD9910:
    """
    AD9910 DDS channel on Urukul.

    This class supports a single DDS channel and exposes the DDS,
    the digital step attenuator, and the RF switch.

    :param chip_select: Chip select configuration. On Urukul this is an
        encoded chip select and not "one-hot": 3 to address multiple chips
        (as configured through CFG_MASK_NU), 4-7 for individual channels.
    :param cpld_device: Name of the Urukul CPLD this device is on.
    :param sw_device: Name of the RF switch device. The RF switch is a
        TTLOut channel available as the :attr:`sw` attribute of this instance.
    :param pll_n: DDS PLL multiplier. The DDS sample clock is
        f_ref/4*pll_n where f_ref is the reference frequency (set in the parent
        Urukul CPLD instance).
    :param pll_cp: DDS PLL charge pump setting.
    :param pll_vco: DDS PLL VCO range selection.
    :param sync_delay_seed: SYNC_IN delay tuning starting value.
        To stabilize the SYNC_IN delay tuning, run :meth:`tune_sync_delay` once
        and set this to the delay tap number returned (default: -1 to signal no
        synchronization and no tuning during :meth:`init`).
    :param io_update_delay: IO_UPDATE pulse alignment delay.
        To align IO_UPDATE to SYNC_CLK, run :meth:`tune_io_update_delay` and
        set this to the delay tap number returned.
    """
    kernel_invariants = {"chip_select", "cpld", "core", "bus",
                         "ftw_per_hz", "pll_n", "io_update_delay",
                         "sysclk_per_mu"}

    def __init__(self, dmgr, chip_select, cpld_device, sw_device=None,
                 pll_n=40, pll_cp=7, pll_vco=5, sync_delay_seed=-1,
                 io_update_delay=0):
        self.cpld = dmgr.get(cpld_device)
        self.core = self.cpld.core
        self.bus = self.cpld.bus
        assert 3 <= chip_select <= 7
        self.chip_select = chip_select
        if sw_device:
            self.sw = dmgr.get(sw_device)
            self.kernel_invariants.add("sw")
        assert 12 <= pll_n <= 127
        self.pll_n = pll_n
        assert self.cpld.refclk/4 <= 60e6
        sysclk = self.cpld.refclk*pll_n/4  # Urukul clock fanout divider
        assert sysclk <= 1e9
        self.ftw_per_hz = (1 << 32)/sysclk
        self.sysclk_per_mu = int(round(sysclk*self.core.ref_period))
        assert self.sysclk_per_mu == sysclk*self.core.ref_period
        assert 0 <= pll_vco <= 5
        vco_min, vco_max = [(370, 510), (420, 590), (500, 700),
                            (600, 880), (700, 950), (820, 1150)][pll_vco]
        assert vco_min <= sysclk/1e6 <= vco_max
        self.pll_vco = pll_vco
        assert 0 <= pll_cp <= 7
        self.pll_cp = pll_cp
        self.sync_delay_seed = sync_delay_seed
        self.io_update_delay = io_update_delay
        self.phase_mode = PHASE_MODE_CONTINUOUS

    @kernel
    def set_phase_mode(self, phase_mode):
        """Set the default phase mode.

        for future calls to :meth:`set` and
        :meth:`set_mu`. Supported phase modes are:

        * :const:`PHASE_MODE_CONTINUOUS`: the phase accumulator is unchanged
          when changing frequency or phase. The DDS phase is the sum of the
          phase accumulator and the phase offset. The only discontinuous
          changes in the DDS output phase come from changes to the phase
          offset. This mode is also knows as "relative phase mode".
          :math:`\phi(t) = q(t^\prime) + p + (t - t^\prime) f`

        * :const:`PHASE_MODE_ABSOLUTE`: the phase accumulator is reset when
          changing frequency or phase. Thus, the phase of the DDS at the
          time of the change is equal to the specified phase offset.
          :math:`\phi(t) = p + (t - t^\prime) f`

        * :const:`PHASE_MODE_TRACKING`: when changing frequency or phase,
          the phase accumulator is cleared and the phase offset is offset
          by the value the phase accumulator would have if the DDS had been
          running at the specified frequency since a given fiducial
          time stamp. This is functionally equivalent to
          :const:`PHASE_MODE_ABSOLUTE`. The only difference is the fiducial
          time stamp. This mode is also known as "coherent phase mode".
          The default fiducial time stamp is 0.
          :math:`\phi(t) = p + (t - T) f`

        Where:

        * :math:`\phi(t)`: the DDS output phase
        * :math:`q(t) = \phi(t) - p`: DDS internal phase accumulator
        * :math:`p`: phase offset
        * :math:`f`: frequency
        * :math:`t^\prime`: time stamp of setting :math:`p`, :math:`f`
        * :math:`T`: fiducial time stamp
        * :math:`t`: running time

        .. warning:: This setting may become inconsistent when used as part of
            a DMA recording. When using DMA, it is recommended to specify the
            phase mode explicitly when calling :meth:`set` or :meth:`set_mu`.
        """
        self.phase_mode = phase_mode

    @kernel
    def write32(self, addr, data):
        """Write to 32 bit register.

        :param addr: Register address
        :param data: Data to be written
        """
        self.bus.set_config_mu(urukul.SPI_CONFIG, 8,
                               urukul.SPIT_DDS_WR, self.chip_select)
        self.bus.write(addr << 24)
        self.bus.set_config_mu(urukul.SPI_CONFIG | spi.SPI_END, 32,
                               urukul.SPIT_DDS_WR, self.chip_select)
        self.bus.write(data)

    @kernel
    def read32(self, addr):
        """Read from 32 bit register.

        :param addr: Register address
        """
        self.bus.set_config_mu(urukul.SPI_CONFIG, 8,
                               urukul.SPIT_DDS_WR, self.chip_select)
        self.bus.write((addr | 0x80) << 24)
        self.bus.set_config_mu(
            urukul.SPI_CONFIG | spi.SPI_END | spi.SPI_INPUT,
            32, urukul.SPIT_DDS_RD, self.chip_select)
        self.bus.write(0)
        return self.bus.read()

    @kernel
    def write64(self, addr, data_high, data_low):
        """Write to 64 bit register.

        :param addr: Register address
        :param data_high: High (MSB) 32 bits of the data
        :param data_low: Low (LSB) 32 data bits
        """
        self.bus.set_config_mu(urukul.SPI_CONFIG, 8,
                               urukul.SPIT_DDS_WR, self.chip_select)
        self.bus.write(addr << 24)
        self.bus.set_config_mu(urukul.SPI_CONFIG, 32,
                               urukul.SPIT_DDS_WR, self.chip_select)
        self.bus.write(data_high)
        self.bus.set_config_mu(urukul.SPI_CONFIG | spi.SPI_END, 32,
                               urukul.SPIT_DDS_WR, self.chip_select)
        self.bus.write(data_low)

    @kernel
    def init(self, blind=False):
        """Initialize and configure the DDS.

        Sets up SPI mode, confirms chip presence, powers down unused blocks,
        configures the PLL, waits for PLL lock. Uses the
        IO_UPDATE signal multiple times.

        :param blind: Do not read back DDS identity and do not wait for lock.
        """
        # Set SPI mode
        self.write32(_AD9910_REG_CFR1, 0x00000002)
        self.cpld.io_update.pulse(1*us)
        delay(1*ms)
        if not blind:
            # Use the AUX DAC setting to identify and confirm presence
            aux_dac = self.read32(_AD9910_REG_AUX_DAC)
            if aux_dac & 0xff != 0x7f:
                raise ValueError("Urukul AD9910 AUX_DAC mismatch")
            delay(50*us)  # slack
        # Configure PLL settings and bring up PLL
        # enable amplitude scale from profiles
        # read effective FTW
        # sync timing validation disable (enabled later)
        self.write32(_AD9910_REG_CFR2, 0x01010020)
        self.cpld.io_update.pulse(1*us)
        cfr3 = (0x0807c100 | (self.pll_vco << 24) |
                (self.pll_cp << 19) | (self.pll_n << 1))
        self.write32(_AD9910_REG_CFR3, cfr3 | 0x400)  # PFD reset
        self.cpld.io_update.pulse(1*us)
        self.write32(_AD9910_REG_CFR3, cfr3)
        self.cpld.io_update.pulse(1*us)
        if blind:
            delay(100*ms)
        else:
            # Wait for PLL lock, up to 100 ms
            for i in range(100):
                sta = self.cpld.sta_read()
                lock = urukul_sta_pll_lock(sta)
                delay(1*ms)
                if lock & (1 << self.chip_select - 4):
                    break
                if i >= 100 - 1:
                    raise ValueError("PLL lock timeout")
        if self.sync_delay_seed >= 0:
            self.tune_sync_delay(self.sync_delay_seed)
        delay(1*ms)

    @kernel
    def power_down(self, bits=0b1111):
        """Power down DDS.

        :param bits: power down bits, see datasheet
        """
        self.write32(_AD9910_REG_CFR1, 0x00000002 | (bits << 4))
        self.cpld.io_update.pulse(1*us)

    @kernel
    def set_mu(self, ftw, pow=0, asf=0x3fff, phase_mode=_PHASE_MODE_DEFAULT,
               ref_time=-1):
        """Set profile 0 data in machine units.

        This uses machine units (FTW, POW, ASF). The frequency tuning word
        width is 32, the phase offset word width is 16, and the amplitude
        scale factor width is 12.

        After the SPI transfer, the shared IO update pin is pulsed to
        activate the data.

        .. seealso: :meth:`set_phase_mode` for a definition of the different
            phase modes.

        :param ftw: Frequency tuning word: 32 bit.
        :param pow: Phase tuning word: 16 bit unsigned.
        :param asf: Amplitude scale factor: 14 bit unsigned.
        :param phase_mode: If specified, overrides the default phase mode set
            by :meth:`set_phase_mode` for this call.
        :param ref_time: Fiducial time used to compute absolute or tracking
            phase updates. In machine units as obtained by `now_mu()`.
        :return: Resulting phase offset word after application of phase
            tracking offset. When using :const:`PHASE_MODE_CONTINUOUS` in
            subsequent calls, use this value as the "current" phase.
        """
        if phase_mode == _PHASE_MODE_DEFAULT:
            phase_mode = self.phase_mode
        # Align to coarse RTIO which aligns SYNC_CLK
        at_mu(now_mu() & ~0xf)
        if phase_mode != PHASE_MODE_CONTINUOUS:
            # Auto-clear phase accumulator on IO_UPDATE.
            # This is active already for the next IO_UPDATE
            self.write32(_AD9910_REG_CFR1, 0x00002002)
            if phase_mode == PHASE_MODE_TRACKING and ref_time < 0:
                # set default fiducial time stamp
                ref_time = 0
            if ref_time >= 0:
                # 32 LSB are sufficient.
                # Also no need to use IO_UPDATE time as this
                # is equivalent to an output pipeline latency.
                dt = int32(now_mu()) - int32(ref_time)
                pow += dt*ftw*self.sysclk_per_mu >> 16
        self.write64(_AD9910_REG_PR0, (asf << 16) | pow, ftw)
        delay_mu(int64(self.io_update_delay))
        self.cpld.io_update.pulse_mu(8)  # assumes 8 mu > t_SYSCLK
        at_mu(now_mu() & ~0xf)
        if phase_mode != PHASE_MODE_CONTINUOUS:
            self.write32(_AD9910_REG_CFR1, 0x00000002)
            # future IO_UPDATE will activate
        return pow

    @portable(flags={"fast-math"})
    def frequency_to_ftw(self, frequency):
        """Return the frequency tuning word corresponding to the given
        frequency.
        """
        return int32(round(self.ftw_per_hz*frequency))

    @portable(flags={"fast-math"})
    def turns_to_pow(self, turns):
        """Return the phase offset word corresponding to the given phase
        in turns."""
        return int32(round(turns*0x10000))

    @portable(flags={"fast-math"})
    def amplitude_to_asf(self, amplitude):
        """Return amplitude scale factor corresponding to given amplitude."""
        return int32(round(amplitude*0x3ffe))

    @portable(flags={"fast-math"})
    def pow_to_turns(self, pow):
        """Return the phase in turns corresponding to a given phase offset
        word."""
        return pow/0x10000

    @kernel
    def set(self, frequency, phase=0.0, amplitude=1.0,
            phase_mode=_PHASE_MODE_DEFAULT, ref_time=-1):
        """Set profile 0 data in SI units.

        .. seealso:: :meth:`set_mu`

        :param ftw: Frequency in Hz
        :param pow: Phase tuning word in turns
        :param asf: Amplitude in units of full scale
        :param phase_mode: Phase mode constant
        :param ref_time: Fiducial time stamp in machine units
        :return: Resulting phase offset in turns
        """
        return self.pow_to_turns(self.set_mu(
            self.frequency_to_ftw(frequency), self.turns_to_pow(phase),
            self.amplitude_to_asf(amplitude), phase_mode, ref_time))

    @kernel
    def set_att_mu(self, att):
        """Set digital step attenuator in machine units.

        .. seealso:: :meth:`artiq.coredevice.urukul.CPLD.set_att_mu`

        :param att: Attenuation setting, 8 bit digital.
        """
        self.cpld.set_att_mu(self.chip_select - 4, att)

    @kernel
    def set_att(self, att):
        """Set digital step attenuator in SI units.

        .. seealso:: :meth:`artiq.coredevice.urukul.CPLD.set_att`

        :param att: Attenuation in dB.
        """
        self.cpld.set_att(self.chip_select - 4, att)

    @kernel
    def cfg_sw(self, state):
        """Set CPLD CFG RF switch state. The RF switch is controlled by the
        logical or of the CPLD configuration shift register
        RF switch bit and the SW TTL line (if used).

        :param state: CPLD CFG RF switch bit
        """
        self.cpld.cfg_sw(self.chip_select - 4, state)

    @kernel
    def set_sync(self, in_delay, window):
        """Set the relevant parameters in the multi device synchronization
        register. See the AD9910 datasheet for details. The SYNC clock
        generator preset value is set to zero, and the SYNC_OUT generator is
        disabled.

        :param in_delay: SYNC_IN delay tap (0-31) in steps of ~75ps
        :param window: Symmetric SYNC_IN validation window (0-15) in
            steps of ~75ps for both hold and setup margin.
        """
        self.write32(_AD9910_REG_MSYNC,
                     (window << 28) |  # SYNC S/H validation delay
                     (1 << 27) |  # SYNC receiver enable
                     (0 << 26) |  # SYNC generator disable
                     (0 << 25) |  # SYNC generator SYS rising edge
                     (0 << 18) |  # SYNC preset
                     (0 << 11) |  # SYNC output delay
                     (in_delay << 3))  # SYNC receiver delay

    @kernel
    def clear_smp_err(self):
        """Clear the SMP_ERR flag and enables SMP_ERR validity monitoring.

        Violations of the SYNC_IN sample and hold margins will result in
        SMP_ERR being asserted. This then also activates the red LED on
        the respective Urukul channel.

        Also modifies CFR2.
        """
        self.write32(_AD9910_REG_CFR2, 0x01010020)  # clear SMP_ERR
        self.cpld.io_update.pulse(1*us)
        self.write32(_AD9910_REG_CFR2, 0x01010000)  # enable SMP_ERR
        self.cpld.io_update.pulse(1*us)

    @kernel
    def tune_sync_delay(self, sync_delay_seed=9):
        """Find a stable SYNC_IN delay.

        This method first locates the smallest SYNC_IN validity window at
        minimum window size and then increases the window a bit to provide some
        slack and stability.

        It starts scanning delays around `sync_delay_seed` (see the
        device database arguments and :class:`AD9910`) at maximum validation
        window size and decreases the window size until a valid delay is found.

        :param sync_delay_seed: Start value for valid SYNC_IN delay search.
        :return: Tuple of optimal delay and window size.
        """
        dt = 14  # 1/(f_SYSCLK*75ps) taps per SYSCLK period
        max_delay = dt  # 14*75ps > 1ns
        max_window = dt//4 + 1  # 75ps*4 = 300ps setup and hold
        min_window = 1  # 1*75ps setup and hold
        for window in range(max_window - min_window + 1):
            window = max_window - window
            for in_delay in range(max_delay):
                # alternate search direction around seed_delay
                if in_delay & 1:
                    in_delay = -in_delay
                in_delay = sync_delay_seed + (in_delay >> 1)
                if in_delay < 0 or in_delay > 31:
                    continue
                self.set_sync(in_delay, window)
                self.clear_smp_err()
                # integrate SMP_ERR statistics for a few hundred cycles
                # delay(10*us)
                err = urukul_sta_smp_err(self.cpld.sta_read())
                delay(40*us)  # slack
                if not (err >> (self.chip_select - 4)) & 1:
                    window -= min_window  # add margin
                    self.set_sync(in_delay, window)
                    self.clear_smp_err()
                    delay(40*us)  # slack
                    return in_delay, window
        raise ValueError("no valid window/delay")

    @kernel
    def measure_io_update_alignment(self, io_up_delay):
        """Use the digital ramp generator to locate the alignment between
        IO_UPDATE and SYNC_CLK.

        The ramp generator is set up to a linear frequency ramp
        (dFTW/t_SYNC_CLK=1) and started at a RTIO time stamp.

        After scanning the alignment, an IO_UPDATE delay midway between two
        edges should be chosen.

        :return: odd/even SYNC_CLK cycle indicator
        """
        # set up DRG
        # DRG ACC autoclear and LRR on io update
        self.write32(_AD9910_REG_CFR1, 0x0000c002)
        # DRG -> FTW, DRG enable
        self.write32(_AD9910_REG_CFR2, 0x01090000)
        # no limits
        self.write64(_AD9910_REG_DRAMPL, -1, 0)
        # DRCTL=0, dt=1 t_SYNC_CLK
        self.write32(_AD9910_REG_DRAMPR, 0x00010000)
        # dFTW = 1, (work around negative slope)
        self.write64(_AD9910_REG_DRAMPS, -1, 0)
        at_mu(now_mu() + 0x10 & ~0xf)  # align to RTIO/2
        self.cpld.io_update.pulse_mu(8)
        # disable DRG autoclear and LRR on io_update
        self.write32(_AD9910_REG_CFR1, 0x00000002)
        # stop DRG
        self.write64(_AD9910_REG_DRAMPS, 0, 0)
        at_mu((now_mu() + 0x10 & ~0xf) + io_up_delay)  # delay
        self.cpld.io_update.pulse_mu(32 - io_up_delay)  # realign
        ftw = self.read32(_AD9910_REG_FTW)  # read out effective FTW
        delay(100*us)  # slack
        # disable DRG
        self.write32(_AD9910_REG_CFR2, 0x01010000)
        self.cpld.io_update.pulse_mu(8)
        return ftw & 1

    @kernel
    def tune_io_update_delay(self):
        """Find a stable IO_UPDATE delay alignment.

        Scan through increasing IO_UPDATE delays until a delay is found that
        lets IO_UPDATE be registered in the next SYNC_CLK cycle. Return a
        IO_UPDATE delay that is midway between two such SYNC_CLK transitions.

        This method assumes that the IO_UPDATE TTLOut device has one machine
        unit resolution (SERDES) and that the ratio between fine RTIO frequency
        (RTIO time machine units) and SYNC_CLK is 4.

        :return: Stable IO_UPDATE delay to be passed to the constructor
            :class:`AD9910` via the device database.
        """
        period = 4  # f_RTIO/f_SYNC = 4
        max_delay = 8  # mu, 1 ns
        d0 = self.io_update_delay
        t0 = int32(self.measure_io_update_alignment(d0))
        for i in range(max_delay - 1):
            t = self.measure_io_update_alignment(
                (d0 + i + 1) & (max_delay - 1))
            if t != t0:
                return (d0 + i + period//2) & (period - 1)
        raise ValueError("no IO_UPDATE-SYNC_CLK alignment edge found")
