# Copyright Amazon.com Inc. or its affiliates. All Rights Reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License"). You
# may not use this file except in compliance with the License. A copy of
# the License is located at
#
#     http://aws.amazon.com/apache2.0/
#
# or in the "license" file accompanying this file. This file is
# distributed on an "AS IS" BASIS, WITHOUT WARRANTIES OR CONDITIONS OF
# ANY KIND, either express or implied. See the License for the specific
# language governing permissions and limitations under the License.


import json
from dataclasses import dataclass
from functools import partial
from unittest import mock
from unittest.mock import Mock

import numpy as np
import pennylane as qml
import pytest
from braket.ahs.analog_hamiltonian_simulation import AnalogHamiltonianSimulation
from braket.ahs.atom_arrangement import AtomArrangement
from braket.ahs.driving_field import DrivingField
from braket.ahs.hamiltonian import Hamiltonian
from braket.ahs.pattern import Pattern
from braket.ahs.shifting_field import ShiftingField
from braket.aws import AwsDevice, AwsQuantumTask
from braket.device_schema import DeviceActionProperties, DeviceActionType
from braket.device_schema.quera.quera_ahs_paradigm_properties_v1 import QueraAhsParadigmProperties
from braket.tasks.analog_hamiltonian_simulation_quantum_task_result import ShotResult
from braket.tasks.local_quantum_task import LocalQuantumTask
from braket.timings.time_series import TimeSeries
from pennylane.pulse.hardware_hamiltonian import HardwarePulse
from pennylane.pulse.parametrized_evolution import ParametrizedEvolution
from pennylane.pulse.rydberg import rydberg_drive, rydberg_interaction

from braket.pennylane_plugin.ahs_device import (
    BraketAhsDevice,
    BraketAwsAhsDevice,
    BraketLocalAhsDevice,
)
from braket.pennylane_plugin.ahs_translation import (
    _convert_to_time_series,
    _create_register,
    _create_valid_local_detunings,
    _evaluate_pulses,
    _extract_pattern_from_detunings,
    _get_sample_times,
    translate_ahs_shot_result,
    translate_pulse_to_driving_field,
    translate_pulses_to_shifting_field,
)

coordinates1 = [[0, 0], [0, 5], [5, 0], [10, 5], [5, 10], [10, 10]]
wires1 = [1, 6, 0, 2, 4, 3]

coordinates2 = [[0, 0], [5.5, 0.0], [2.75, 4.763139720814412]]  # in µm
H_i = rydberg_interaction(coordinates2)


def f1(p, t):
    return p * np.sin(t) * (t - 1)


def f2(p, t):
    return p[0] * np.cos(p[1] * t**2)


def amp(p, t):
    return p[0] * np.exp(-((t - p[1]) ** 2) / (2 * p[2] ** 2))


# functions of time to use as partially evaluated callable parameters in tests
def sin_fn(t):
    return np.sin(t)


def sin_squared(t):
    return np.sin(t) ** 2


def cos_fn(t):
    return np.cos(t)


def lin_fn(t):
    return 3.48 * t


def quad_fn(t):
    return 4.5 * t**2


def dummy_cfunc(t):
    """Dummy function for testing local detunings"""
    return 10


params1 = 1.2
params2 = [3.4, 5.6]
params_amp = [2.5, 0.9, 0.3]

HAMILTONIANS_AND_PARAMS = [
    (H_i + rydberg_drive(amplitude=4, phase=1, detuning=3, wires=[0, 1, 2]), []),
    (H_i + rydberg_drive(amplitude=amp, phase=1, detuning=2, wires=[0, 1, 2]), [params_amp]),
    (H_i + rydberg_drive(amplitude=2, phase=f1, detuning=2, wires=[0, 1, 2]), [params1]),
    (H_i + rydberg_drive(amplitude=2, phase=2, detuning=f2, wires=[0, 1, 2]), [params2]),
    (
        H_i + rydberg_drive(amplitude=amp, phase=1, detuning=f2, wires=[0, 1, 2]),
        [params_amp, params2],
    ),
    (H_i + rydberg_drive(amplitude=4, phase=f2, detuning=f1, wires=[0, 1, 2]), [params2, params1]),
    (
        H_i + rydberg_drive(amplitude=amp, phase=f2, detuning=4, wires=[0, 1, 2]),
        [params_amp, params2],
    ),
    (
        H_i + rydberg_drive(amplitude=amp, phase=f2, detuning=f1, wires=[0, 1, 2]),
        [params_amp, params2, params1],
    ),
]


DEV_ATTRIBUTES = [(BraketAwsAhsDevice, "Aquila", "braket.aws.ahs")]

dev_sim = BraketLocalAhsDevice(wires=3, shots=17)


PARADIGM_PROPERTIES = QueraAhsParadigmProperties.parse_raw_schema(
    json.dumps(
        {
            "braketSchemaHeader": {
                "name": "braket.device_schema.quera.quera_ahs_paradigm_properties",
                "version": "1",
            },
            "qubitCount": 256,
            "lattice": {
                "area": {"width": 0.000075, "height": 0.000076},
                "geometry": {
                    "spacingRadialMin": 0.000004,
                    "spacingVerticalMin": 0.000004,
                    "positionResolution": 1e-7,
                    "numberSitesMax": 256,
                },
            },
            "rydberg": {
                "c6Coefficient": 5.42e-24,
                "rydbergGlobal": {
                    "rabiFrequencyRange": (0, 15800000.0),
                    "rabiFrequencyResolution": 400.0,
                    "rabiFrequencySlewRateMax": 250000000000000.0,
                    "detuningRange": (-125000000.0, 125000000.0),
                    "detuningResolution": 0.2,
                    "detuningSlewRateMax": 2500000000000000.0,
                    "phaseRange": (-99.0, 99.0),
                    "phaseResolution": 5e-7,
                    "timeResolution": 1e-9,
                    "timeDeltaMin": 5e-8,
                    "timeMin": 0,
                    "timeMax": 0.000004,
                },
            },
            "performance": {
                "lattice": {"positionErrorAbs": 1.47e-7},
                "rydberg": {"rydbergGlobal": {"rabiFrequencyErrorRel": 0.02}},
            },
        }
    )
)


class MockAwsSession:
    @staticmethod
    def add_braket_user_agent(user_agent):
        pass


class MockDevProperties:
    paradigm = PARADIGM_PROPERTIES
    action = {
        DeviceActionType.AHS: DeviceActionProperties(version=["1"], actionType=DeviceActionType.AHS)
    }


@pytest.fixture(scope="function")
def mock_aws_device(monkeypatch, wires=3):
    """A function to create a mock device that mocks most of the methods"""
    with monkeypatch.context() as m:
        m.setattr(AwsDevice, "__init__", lambda self, *args, **kwargs: None)
        m.setattr(AwsDevice, "aws_session", MockAwsSession)
        m.setattr(AwsDevice, "type", mock.PropertyMock)
        m.setattr(AwsDevice, "properties", MockDevProperties)

        def get_aws_device(
            wires=wires,
            shots=17,
            device_arn="baz",
            **kwargs,
        ):
            dev = BraketAwsAhsDevice(
                wires=wires,
                s3_destination_folder=("foo", "bar"),
                device_arn=device_arn,
                aws_session=Mock(),
                shots=shots,
                **kwargs,
            )
            # needed by BraketAwsAhsDevice functions
            dev._device._arn = device_arn
            dev._device._aws_session = Mock()
            return dev

        yield get_aws_device


def dummy_ahs_program():
    # amplutide 10 for full duration
    amplitude = TimeSeries()
    amplitude.put(0, 10)
    amplitude.put(4e-6, 10)

    # phase and detuning 0 for full duration
    phi = TimeSeries().put(0, 0).put(4e-6, 0)
    detuning = TimeSeries().put(0, 0).put(4e-6, 0)

    # Hamiltonian
    H = DrivingField(amplitude, phi, detuning)

    # register
    register = AtomArrangement()
    for [x, y] in coordinates2:
        register.add([x * 1e-6, y * 1e-6])

    ahs_program = AnalogHamiltonianSimulation(hamiltonian=H, register=register)

    return ahs_program


# dummy data classes for testing result processing
@dataclass
class Status:
    value: str


@dataclass
class DummyMeasurementResult:
    status: Status
    pre_sequence: np.array
    post_sequence: np.array


DUMMY_RESULTS = [
    (DummyMeasurementResult(Status("Success"), np.array([1]), np.array([1])), np.array([0])),
    (DummyMeasurementResult(Status("Success"), np.array([1]), np.array([0])), np.array([1])),
    (DummyMeasurementResult(Status("Success"), np.array([0]), np.array([0])), np.array([np.NaN])),
    (DummyMeasurementResult(Status("Failure"), np.array([1]), np.array([1])), np.array([np.NaN])),
    (
        DummyMeasurementResult(Status("Success"), np.array([1, 1, 0]), np.array([1, 0, 0])),
        np.array([0, 1, np.NaN]),
    ),
    (
        DummyMeasurementResult(Status("Success"), np.array([1, 1]), np.array([0, 0])),
        np.array([1, 1]),
    ),
    (
        DummyMeasurementResult(Status("Success"), np.array([0, 1]), np.array([0, 0])),
        np.array([np.NaN, 1]),
    ),
    (
        DummyMeasurementResult(Status("Failure"), np.array([1, 1]), np.array([1, 1])),
        np.array([np.NaN, np.NaN]),
    ),
    (
        DummyMeasurementResult(Status("Success"), np.array([0, 1]), np.array([1, 0])),
        np.array([np.NaN, 1]),
    ),
]


class TestBraketAhsDevice:
    """Tests that behaviour defined for both the LocalSimulator and the
    Aquila hardware in the base device work as expected"""

    def test_initialization(self):
        """Test the device initializes with the expected attributes"""

        dev = BraketLocalAhsDevice(wires=3, shots=11)

        assert dev._device.name == "RydbergAtomSimulator"
        assert dev.short_name == "braket.local.ahs"
        assert dev.shots == 11
        assert dev.ahs_program is None
        assert dev.result is None
        assert dev.pennylane_requires == ">=0.30.0"
        assert dev.operations == {"ParametrizedEvolution"}

    def test_settings(self):
        dev = dev_sim
        assert isinstance(dev.settings, dict)
        assert "interaction_coeff" in dev.settings.keys()
        assert len(dev.settings.keys()) == 1
        assert dev.settings["interaction_coeff"] == 862620

    def test_run_task_not_implemented(self):
        """Test that the _run_task method raises a NotImplemented error in the base class"""

        dev = BraketAhsDevice(wires=2, device=None)
        ahs_program = dummy_ahs_program()
        with pytest.raises(NotImplementedError, match="not implemented for the base class"):
            dev._run_task(ahs_program)

    @pytest.mark.parametrize(
        "dev_cls, shots", [(BraketLocalAhsDevice, 1000), (BraketLocalAhsDevice, 2)]
    )
    def test_setting_shots(self, dev_cls, shots):
        """Test that setting shots changes number of shots from default (100)"""
        dev = dev_cls(wires=3, shots=shots)
        assert dev.shots == shots

    @pytest.mark.parametrize("shots", [0, None])
    def test_no_shots_raises_error(self, shots):
        """Test that an error is raised if shots are set to 0 or None"""
        with pytest.raises(RuntimeError, match="This device requires shots"):
            BraketLocalAhsDevice(wires=3, shots=shots)

    @pytest.mark.parametrize(
        "dev_cls, wires",
        [
            (BraketLocalAhsDevice, 2),
            (BraketLocalAhsDevice, [0, 2, 4]),
            (BraketLocalAhsDevice, [0, "a", 7]),
            (BraketLocalAhsDevice, 7),
        ],
    )
    def test_setting_wires(self, dev_cls, wires):
        """Test setting wires"""
        dev = dev_cls(wires=wires)

        if isinstance(wires, int):
            assert len(dev.wires) == wires
            assert dev.wires.labels == tuple(i for i in range(wires))
        else:
            assert len(wires) == len(dev.wires)
            assert dev.wires.labels == tuple(wires)

    @pytest.mark.parametrize("hamiltonian, params", HAMILTONIANS_AND_PARAMS)
    def test_apply(self, hamiltonian, params):
        """Test that apply creates and saves an ahs_program and restuls as expected"""
        t = 0.4
        operations = [ParametrizedEvolution(hamiltonian, params, t)]
        dev = BraketLocalAhsDevice(wires=operations[0].wires)

        assert dev._task is None
        assert dev.task is None
        assert dev.result is None
        assert dev.ahs_program is None

        # Need to run dev._validate_pulses to set dev.global_pulse_idx
        dev._validate_pulses(operations[0].H.pulses)
        dev.apply(operations)

        assert dev.result is not None
        assert dev.task is not None
        assert dev.task == dev._task
        assert len(dev.result.measurements) == dev.shots
        assert len(dev.result.measurements[0].pre_sequence) == len(dev.wires)

        assert isinstance(dev.ahs_program, AnalogHamiltonianSimulation)
        assert dev.ahs_program.register == dev.register
        assert dev.ahs_program.hamiltonian.amplitude.time_series.times()[-1] == t * 1e-6

    def test_check_validity_unsupported_op(self):
        """Tests that check_validity() throws NotImplementedError when it encounters
        an unknown gate."""

        with pytest.raises(NotImplementedError):
            dev_sim.check_validity([qml.PauliX(0)], [])

    @pytest.mark.parametrize("H, params", HAMILTONIANS_AND_PARAMS)
    def test_check_validity_valid_circuit(self, H, params):
        """Tests that check_validity() doesn't raise any errors when the operations and
        observables are valid."""
        ops = [ParametrizedEvolution(H, params, [0, 1.5])]
        obs = [
            qml.PauliZ(0),
            qml.expval(qml.PauliZ(0)),
            qml.var(qml.Identity(0)),
            qml.sample(qml.PauliZ(0)),
            qml.prod(qml.PauliZ(0), qml.Identity(1)),
            qml.counts(),
        ]
        dev = qml.device("braket.local.ahs", wires=3)

        dev.check_validity(ops, obs)

    @pytest.mark.parametrize("H, params", HAMILTONIANS_AND_PARAMS)
    def test_check_validity_raises_error_for_state_based_measurement(self, H, params):
        """Tests that requesting a measurement other than a sample-based
        measurement raises an error"""

        dev = qml.device("braket.local.ahs", wires=3)

        ops = [ParametrizedEvolution(H, params, [0, 1.5])]
        obs = [qml.state()]

        with pytest.raises(RuntimeError, match="only support sample-based measurement"):
            dev.check_validity(ops, obs)

    @pytest.mark.parametrize("hamiltonian, params", HAMILTONIANS_AND_PARAMS)
    def test_create_ahs_program(self, hamiltonian, params):
        """Test that we can create an AnalogueHamiltonianSimulation from an
        evolution operator and store it on the device"""

        evolution = ParametrizedEvolution(hamiltonian, params, 1.5)
        dev = BraketLocalAhsDevice(wires=3)

        assert dev.ahs_program is None

        dev._validate_pulses(evolution.H.pulses)
        ahs_program = dev.create_ahs_program(evolution)

        # AHS program is created and stored on the device
        assert isinstance(dev.ahs_program, AnalogHamiltonianSimulation)

        # compare evolution and ahs_program registers
        assert ahs_program.register.coordinate_list(0) == [
            c[0] * 1e-6 for c in evolution.H.settings.register
        ]
        assert ahs_program.register.coordinate_list(1) == [
            c[1] * 1e-6 for c in evolution.H.settings.register
        ]

        # elements of the hamiltonian have the expected shape
        h = ahs_program.hamiltonian
        amp_time, amp_vals = h.amplitude.time_series.times(), h.amplitude.time_series.values()
        phase_time, phase_vals = h.phase.time_series.times(), h.phase.time_series.values()
        det_time, det_vals = h.detuning.time_series.times(), h.detuning.time_series.values()

        assert amp_time == phase_time == det_time
        assert amp_time[0] == evolution.t[0] * 1e-6
        assert amp_time[-1] == evolution.t[1] * 1e-6

        pulse = hamiltonian.pulses[0]
        params_idx = 0

        if callable(pulse.amplitude):
            fn = pulse.amplitude
            p = params[params_idx]
            params_idx += 1
            assert np.allclose([fn(p, t * 1e6) * 2 * np.pi * 1e6 for t in amp_time], amp_vals)
        else:
            assert np.allclose([pulse.amplitude * 2 * np.pi * 1e6 for t in amp_time], amp_vals)

        if callable(pulse.phase):
            fn = pulse.phase
            p = params[params_idx]
            params_idx += 1
            assert np.allclose([fn(p, t * 1e6) for t in amp_time], phase_vals)
        else:
            assert np.allclose([pulse.phase for t in amp_time], phase_vals)

        if callable(pulse.frequency):
            fn = pulse.frequency
            p = params[params_idx]
            params_idx += 1
            assert np.allclose([fn(p, t * 1e6) * 2 * np.pi * 1e6 for t in amp_time], det_vals)
        else:
            assert np.allclose([pulse.frequency * 2 * np.pi * 1e6 for t in amp_time], det_vals)

    def test_generate_samples(self):
        """Test that generate_samples creates a list of arrays with the expected shape for the
        task run"""
        ahs_program = dummy_ahs_program()
        dev = qml.device("braket.local.ahs", wires=3)

        # checked in _validate_operations in the full pipeline
        # since these are created manually for the unit test elsewhere in the file,
        # we confirm the values used for the test are valid here
        assert len(ahs_program.register.coordinate_list(0)) == len(dev.wires)

        task = dev._run_task(ahs_program)

        dev._task = task
        samples = dev.generate_samples()

        assert len(samples) == 1000
        assert len(samples[0]) == len(dev.wires)
        assert isinstance(samples[0], np.ndarray)

    def test_expval_handles_nan(self):
        """Test that expval takes the average ignoring NaN values"""

        dev = qml.device("braket.local.ahs", wires=4, shots=4)

        dev._samples = np.array(
            [
                [0, 1, 1, np.NaN],
                [1, 1, 0, 0],
                [1, 0, 0, 1],
                [0, 1, 1, 1],
            ]
        )

        res = dev.expval(qml.PauliZ(3))

        assert res != np.NaN

    def test_no_diagonalzing_gates_raises_error(self):
        """Tests that if passed an Operator with no diagonalizing gates,
        a suitable error message is raised in _validate_measurement_basis"""

        dev = qml.device("braket.local.ahs", wires=3)

        with pytest.raises(
            RuntimeError, match="with no diagonalizing gates; cannot determine basis"
        ):
            dev._validate_measurement_basis(qml.CNOT([0, 1]))

    @pytest.mark.parametrize(
        "observable, error_expected",
        [
            (qml.PauliX(0), True),
            (qml.PauliZ(0), False),
            (qml.Projector([0], wires=[0]), False),
            (qml.Projector(np.array([1.0, 1.0]) / np.sqrt(2), wires=[0]), True),
            (qml.sum(qml.PauliZ(0), qml.PauliZ(0)), False),  # sum
            (qml.sum(qml.PauliZ(0), qml.PauliY(0)), True),
            (qml.s_prod(3, qml.PauliY(0)), True),  # scalar prod
            (qml.s_prod(-1, qml.Projector([0], wires=[0])), False),
            (qml.prod(qml.PauliZ(0), qml.PauliZ(1)), False),  # product
            (qml.prod(qml.PauliY(2), qml.PauliX(1)), True),
            (qml.exp(qml.PauliY(1), 2), True),  # exp
            (qml.exp(qml.prod(qml.PauliZ(0), qml.Identity(1)), 3), False),
            (qml.Hamiltonian([2, 3], [qml.PauliZ(0), qml.PauliZ(1)]), False),
            (qml.Hamiltonian([2, 3], [qml.PauliZ(0), qml.PauliY(1)]), True),
            (
                qml.sum(
                    qml.prod(qml.PauliZ(0), qml.Projector([0], wires=[1])),
                    qml.prod(qml.Projector([0], wires=[5]), qml.PauliZ(1)),
                ),
                False,
            ),  # sum of prods
            (
                qml.sum(
                    qml.prod(qml.PauliX(0), qml.Projector([0], wires=[1])),
                    qml.prod(qml.Projector([0], wires=[5]), qml.PauliZ(1)),
                ),
                True,
            ),
        ],
    )
    def test_validate_measurement_basis(self, observable, error_expected):
        """Tests that when given an Observable not in the Z basis, _validate_measurement_basis,
        fails with an error, but otherwise passes"""

        dev = qml.device("braket.local.ahs", wires=3)

        if error_expected:
            with pytest.raises(RuntimeError, match="can only measure in the Z basis"):
                dev._validate_measurement_basis(observable)
        else:
            dev._validate_measurement_basis(observable)

    def test_validate_measurement_basis_large_observable(self):
        """Test _validate_measurement_basis for an observable composed of many
        elements and many layers of CompositeOps, with a large matrix"""

        a = 6.7

        coords = [
            [0, 0],
            [0, a],
            [a / 2, a + np.sqrt(3) / 2 * a],
            [-a / 2, a + np.sqrt(3) / 2 * a],
            [-a, 0],
            [0, -a],
            [a / 2, -a - np.sqrt(3) / 2 * a],
            [-a / 2, -a - np.sqrt(3) / 2 * a],
        ]

        edges = [[1, 2], [2, 3], [3, 1], [1, 0], [0, 4], [0, 5], [5, 6], [6, 7], [7, 5]]

        # nested operator of sums of Projectors and products of sums etc with several layers
        H_edges = qml.Identity(wires=range(len(coords)))
        for ind_edge, edge in enumerate(edges):
            H_edge = qml.prod(
                qml.Projector([0], wires=[edge[0]]), qml.Projector([0], wires=[edge[1]])
            )
            H_edge += qml.prod(
                qml.Projector([0], wires=[edge[0]]), qml.Projector([1], wires=[edge[1]])
            )
            H_edge += qml.prod(
                qml.Projector([1], wires=[edge[0]]), qml.Projector([0], wires=[edge[1]])
            )
            H_edges = qml.prod(H_edges, H_edge)

        H_vertices = 0
        for i in range(len(coords)):
            H_vertices += -1 * qml.Projector([1], wires=[i])

        # creates product of a Hamiltonian and the above H_edges operator
        H_cost = qml.prod(H_vertices, H_edges)

        # we expect the function to pass without raising an error
        dev = qml.device("braket.local.ahs", wires=3)
        dev._validate_measurement_basis(H_cost)

    def test_observable_not_in_z_basis_raises_error(self):
        """Test that measuring an observable not in
        the computational basis raises an error"""

        dev = qml.device("braket.local.ahs", wires=3)

        with pytest.raises(RuntimeError, match="can only measure in the Z basis"):
            dev._validate_measurement_basis(qml.PauliX(0))

    def test_validate_operations_multiple_operators(self):
        """Test that an error is raised if there are multiple operators"""

        H1 = rydberg_drive(amp, f1, 2, wires=[0, 1, 2])
        op1 = qml.evolve(H_i + H1)
        op2 = qml.evolve(H_i + H1)

        with pytest.raises(
            NotImplementedError, match="Support for multiple ParametrizedEvolution operators"
        ):
            dev_sim._validate_operations([op1, op2])

    def test_validate_operations_wires_match_device(self):
        """Test that an error is raised if the wires on the Hamiltonian
        don't match the wires on the device."""
        H = H_i + rydberg_drive(3, 2, 2, wires=[0, 1, 2])

        dev1 = BraketLocalAhsDevice(wires=len(H.wires) - 1)
        dev2 = BraketLocalAhsDevice(wires=len(H.wires) + 1)

        with pytest.raises(RuntimeError, match="Device wires must match wires of the evolution."):
            dev1._validate_operations([ParametrizedEvolution(H, [], 1)])

        with pytest.raises(RuntimeError, match="Device wires must match wires of the evolution."):
            dev2._validate_operations([ParametrizedEvolution(H, [], 1)])

    def test_validate_operations_register_matches_wires(self):
        """Test that en error is raised in the length of the register doesn't match
        the number of wires on the device"""

        # register has wires [0, 1, 2], drive has wire [3]
        # creating a Hamiltonian like this in PL will raise a warning, but not an error
        H = H_i + rydberg_drive(3, 2, 2, wires=3)

        # device wires [0, 1, 2, 3] match overall wires, but not length of register
        dev = BraketLocalAhsDevice(wires=4)

        with pytest.raises(RuntimeError, match="The defined interaction term has register"):
            dev._validate_operations([ParametrizedEvolution(H, [], 1)])

    def test_validate_operations_not_hardware_hamiltonian(self):
        """Test that an error is raised if the ParametrizedHamiltonian on the operator
        is not a HardwareHamiltonian and so does not contain pulse upload information"""

        H1 = 2 * qml.PauliX(0) + f1 * qml.PauliY(1) + f2 * qml.PauliZ(2)
        op1 = qml.evolve(H1)

        with pytest.raises(RuntimeError, match="Expected a HardwareHamiltonian instance"):
            dev_sim._validate_operations([op1])

    def test_validate_pulses_no_pulses(self, mock_aws_device):
        """Test that _validate_pulses raises an error if there are no pulses saved
        on the Hamiltonian"""
        dev = mock_aws_device()

        with pytest.raises(RuntimeError, match="No pulses found"):
            dev._validate_pulses(H_i.pulses)

    @pytest.mark.parametrize("coordinates", [coordinates1, coordinates2])
    def test_create_register(self, coordinates):
        """Test that an AtomArrangement with the expected coordinates is created
        and stored on the device"""

        dev = BraketLocalAhsDevice(wires=len(coordinates))

        assert dev.register is None

        dev._register = _create_register(coordinates)

        coordinates_from_register = [
            [x * 1e6, y * 1e6]
            for x, y in zip(dev.register.coordinate_list(0), dev.register.coordinate_list(1))
        ]

        assert isinstance(dev.register, AtomArrangement)
        assert coordinates_from_register == coordinates

    @pytest.mark.parametrize("hamiltonian, params", HAMILTONIANS_AND_PARAMS)
    def test_evaluate_pulses(self, hamiltonian, params):
        """Test that the callables describing pulses are partially evaluated as expected"""

        ev_op = ParametrizedEvolution(hamiltonian, params, 1.5)

        pulse = ev_op.H.pulses[0]
        params = ev_op.parameters
        idx = 0

        # check which of initial pulse parameters are callable
        callable_amp = callable(pulse.amplitude)
        callable_phase = callable(pulse.phase)
        callable_detuning = callable(pulse.frequency)

        # get an expected value for each pulse parameter at t=1.7
        if callable_amp:
            amp_sample = pulse.amplitude(params[idx], 1.7)
            idx += 1
        else:
            amp_sample = pulse.amplitude

        if callable_phase:
            phase_sample = pulse.phase(params[idx], 1.7)
            idx += 1
        else:
            phase_sample = pulse.phase

        if callable_detuning:
            detuning_sample = pulse.frequency(params[idx], 1.7)
            idx += 1
        else:
            detuning_sample = pulse.frequency

        # evaluate pulses
        dev_sim._pulses = _evaluate_pulses(ev_op)

        # confirm that if initial pulse parameter was a callable, it is now a partial
        # confirm that post-evaluation value at t=1.7 seconds matches expectation
        if callable_amp:
            assert isinstance(dev_sim._pulses[0].amplitude, partial)
            assert amp_sample == dev_sim._pulses[0].amplitude(1.7)
        else:
            assert amp_sample == dev_sim._pulses[0].amplitude

        if callable_phase:
            assert isinstance(dev_sim._pulses[0].phase, partial)
            assert phase_sample == dev_sim._pulses[0].phase(1.7)
        else:
            assert phase_sample == dev_sim._pulses[0].phase

        if callable_detuning:
            assert isinstance(dev_sim._pulses[0].frequency, partial)
            assert detuning_sample == dev_sim._pulses[0].frequency(1.7)
        else:
            assert detuning_sample == dev_sim._pulses[0].frequency

    @pytest.mark.parametrize("time_interval", [[1.5, 2.3], [0, 1.2], [0.111, 3.789]])
    def test_get_sample_times(self, time_interval):
        """Tests turning an array of [start, end] times into time set-points"""

        times = _get_sample_times(time_interval)

        num_points = len(times)
        diffs = np.array([times[i] - times[i - 1] for i in range(1, num_points)])
        diffs = np.around(diffs, decimals=9)  # precision level is ns

        # start and end times match but are in units of s and us respectively
        assert times[0] * 1e6 == time_interval[0]
        assert times[-1] * 1e6 == time_interval[1]

        # distances between points are close to 50ns
        assert np.all(d >= 50e-9 for d in diffs)
        assert np.allclose(diffs, 50e-9, atol=5e-9)

    def test_convert_to_time_series_constant(self):
        """Test creating a TimeSeries when the pulse parameter is defined as a constant float"""

        times = [0, 1, 2, 3, 4, 5]
        ts = _convert_to_time_series(pulse_parameter=4.3, time_points=times)

        assert ts.times() == times
        assert all(p == 4.3 for p in ts.values())

    def test_convert_to_time_series_callable(self):
        """Test creating a TimeSeries when the pulse parameter is defined as a function of time"""

        def f(t):
            return np.sin(t)

        times_us = [0, 1, 2, 3, 4, 5]  # microseconds
        times_s = [t * 1e-6 for t in times_us]  # seconds

        ts = _convert_to_time_series(pulse_parameter=f, time_points=times_s)
        expected_vals = [np.sin(t) for t in times_us]

        assert ts.times() == times_s
        assert np.all(ts.values() == expected_vals)

    def test_convert_to_time_series_scaling_factor(self):
        """Test creating a TimeSeries from pulse information and time set-points"""

        def f(t):
            return np.sin(t)

        times_us = [0, 1, 2, 3, 4, 5]  # microseconds
        times_s = [t * 1e-6 for t in times_us]  # seconds

        ts = _convert_to_time_series(pulse_parameter=f, time_points=times_s, scaling_factor=1.7)
        expected_vals = [np.sin(t) * 1.7 for t in times_us]

        assert ts.times() == times_s
        assert ts.values() == expected_vals

    @pytest.mark.parametrize(
        "pulse",
        [
            HardwarePulse(1, 2, sin_fn, wires=[0, 1, 2]),
            HardwarePulse(cos_fn, 1.7, 2.3, wires=[0, 1, 2]),
            HardwarePulse(3.8, lin_fn, 1.9, wires=[0, 1, 2]),
            HardwarePulse(lin_fn, sin_fn, quad_fn, wires=[0, 1, 2]),
        ],
    )
    def test_translate_pulse_to_driving_field(self, pulse):
        """Test that a time interval in microseconds (as passed to the qnode in PennyLane)
        and a Pulse object containing constant or time-dependent pulse parameters (floats
        and/or callables that have been evaluated to be a function only of time)
        and can be converted into a DrivingField
        """

        drive = translate_pulse_to_driving_field(pulse, [0, 1.5])

        assert isinstance(drive, DrivingField)

    @pytest.mark.parametrize("res, expected_output", DUMMY_RESULTS)
    def test_result_to_sample_output(self, res, expected_output):
        """Test function for converting the task results as returned by the
        device into sample measurement results for PennyLane"""

        output = translate_ahs_shot_result(res)

        assert isinstance(output, np.ndarray)
        assert len(output) == len(res.post_sequence)
        assert np.allclose(output, expected_output, equal_nan=True)


class TestLocalAhsDevice:
    """Test functionality specific to the local simulator device"""

    @pytest.mark.parametrize(
        "pulses, error",
        [
            (
                [HardwarePulse(3, 4, 5, [0, 1, 2]), HardwarePulse(4, 6, 7, [1, 0, 2])],
                "ParametrizedEvolution with multiple global drives",
            ),
            (
                [HardwarePulse(3, 4, 5, [3, 4])],
                "which are not a subset of device wires",
            ),
            (
                [HardwarePulse(3, 4, 5, [0])],
                "doesn't apply a global driving field to all wires",
            ),
            ([], "doesn't apply a global driving field to all wires"),
            (
                [HardwarePulse(3, 4, 5, [0]), HardwarePulse(3, 4, 5, [0, 1, 2])],
                "Amplitude must be zero.",
            ),
            (
                [
                    HardwarePulse(3, 4, 5, [0, 1, 2]),
                    HardwarePulse(0, 0, f1, [0]),
                    HardwarePulse(0, 0, 2, [0]),
                ],
                "Found local pulses with both `float` and `callable` detunings.",
            ),
            (
                [
                    HardwarePulse(3, 4, 5, [0, 1, 2]),
                    HardwarePulse(0, 0, 2, [0, 1]),
                    HardwarePulse(0, 0, 4, [0, 2]),
                ],
                "Local drives must not have overlapping wires.",
            ),
        ],
    )
    def test_invalid_pulses(self, pulses, error):
        """Test that invalid pulses raise the correct errors during validation"""
        with pytest.raises(ValueError, match=error):
            dev_sim._validate_pulses(pulses)

    @pytest.mark.parametrize(
        "pulses",
        (
            [HardwarePulse(3, 4, 5, [0, 1, 2])],
            [
                HardwarePulse(3, 4, 5, [0, 1, 2]),
                HardwarePulse(0, 0, f1, [1]),
                HardwarePulse(0, 0, f2, [2]),
            ],
            [
                HardwarePulse(0, 0, 3.5, [0]),
                HardwarePulse(0, 0, 5.4, [2]),
                HardwarePulse(3, 4, 5, [0, 1, 2]),
            ],
        ),
    )
    def test_validate_pulses_valid_pulses(self, pulses):
        """Test that `_validate_pulses` does not raise any errors when the pulses are valid."""
        dev_sim._validate_pulses(pulses)

    def test_run_task(self):
        """Test that `run_task` returns the correct objects with the number of measurements
        equal to the number of shots."""
        ahs_program = dummy_ahs_program()

        task = dev_sim._run_task(ahs_program)

        assert isinstance(task, LocalQuantumTask)
        assert len(task.result().measurements) == 17  # dev_sim takes 17 shots
        assert isinstance(task.result().measurements[0], ShotResult)

    @pytest.mark.parametrize(
        "pulses, expected_detunings",
        [
            ([HardwarePulse(0, 0, 2, [0, 1])], [2, 2, 0]),
            (
                [
                    HardwarePulse(0, 0, dummy_cfunc, [0]),
                    HardwarePulse(0, 0, dummy_cfunc, [1, 2]),
                ],
                [dummy_cfunc, dummy_cfunc, dummy_cfunc],
            ),
            (
                [
                    HardwarePulse(0, 0, 4, [0]),
                    HardwarePulse(0, 0, 2, [1, 2]),
                ],
                [4, 2, 2],
            ),
            (
                [HardwarePulse(0, 0, dummy_cfunc, [0, 1])],
                [dummy_cfunc, dummy_cfunc, lambda t: 0],
            ),
        ],
    )
    def test_create_valid_local_detunings(self, pulses, expected_detunings):
        """Test that BraketLocalAhsDevice._create_valid_local_detunings expands and pads the
        detunings so that there is one local detuning per device wire, and that the detunings
        are mapped correctly."""

        detunings = _create_valid_local_detunings(pulses, dev_sim.wires)

        assert len(detunings) == len(dev_sim.wires)

        if callable(pulses[0].frequency):
            for det, expected_det in zip(detunings, expected_detunings):
                for i in range(10):
                    assert det(i) == expected_det(i)
        else:
            assert all(detunings[i] == expected_detunings[i] for i in range(len(dev_sim.wires)))

    def test_create_valid_local_detunings_no_local_detunings(self):
        """Test that _create_valid_local_detunings returns `None` if there are no local
        detunings."""

        valid_detunings = _create_valid_local_detunings([], dev_sim.wires)
        assert valid_detunings is None

    def test_extract_pattern_from_detunings_mismatched_detuning(self):
        """Test that an error is raised when the shapes of the local detunings
        don't match."""
        detunings = [lambda t: np.sin(t) ** 2, lambda t: np.cos(t) ** 2]
        time_interval = [0, 20]
        time_points = _get_sample_times(time_interval)

        with pytest.raises(ValueError, match="Local detunings don't match"):
            _ = _extract_pattern_from_detunings(detunings, time_points)

    @pytest.mark.parametrize(
        "detunings", ([lambda t: np.sin(t), lambda t: 0.5 * np.sin(t)], [-1, 2])
    )
    def test_extract_pattern_from_detuning_negative_detuning(self, detunings):
        """Test that negative values in local detunings raise the correct error."""
        with pytest.raises(ValueError, match="Found negative value in local detunings"):
            _ = _extract_pattern_from_detunings(detunings, _get_sample_times([0, 20]))

    @pytest.mark.parametrize(
        "detunings, expected_max, expected_pattern",
        [
            ([3, 2, 1], 3, [1, 2 / 3, 1 / 3]),
            ([lambda t: 2, dummy_cfunc, lambda t: 0], dummy_cfunc, [0.2, 1, 0]),
            (
                [sin_squared, lambda t: 0.5 * sin_squared(t), lambda t: 0.333 * sin_squared(t)],
                sin_squared,
                [1, 0.5, 0.333],
            ),
            ([0, 0, 0], 0, [1, 1, 1]),
            ([lambda t: 0] * 3, 0, [1, 1, 1]),
        ],
    )
    def test_extract_pattern_from_detunings(self, detunings, expected_max, expected_pattern):
        """Test that BraketLocalAhsDevice._extract_pattern_from_detunings
        finds the pattern from valid local detunings correctly."""
        max_detuning, pattern = _extract_pattern_from_detunings(
            detunings, _get_sample_times([0, 20])
        )

        assert max_detuning == expected_max
        assert isinstance(pattern, Pattern)
        assert np.allclose(pattern.series, expected_pattern)

    @pytest.mark.parametrize(
        "detunings, pattern",
        [
            (
                [lambda t: sin_squared(t), lambda t: sin_squared(t), lambda t: 5 * sin_squared(t)],
                [0.2, 0.2, 1],
            ),
            ([1, 8.9, 10], [0.1, 0.89, 1]),
        ],
    )
    def test_convert_pulses_to_shifting_field(self, detunings, pattern):
        """Test that BraketLocalAhsDevice._convert_pulses_to_shifting_field
        creates a valid `ShiftingField`."""
        times = _get_sample_times([0, 20])
        shift = translate_pulses_to_shifting_field(detunings, times)
        assert isinstance(shift, ShiftingField)
        assert np.allclose(shift.magnitude.pattern.series, pattern)

        shift_times = shift.magnitude.time_series.times()
        assert np.allclose(shift_times, times)

    @pytest.mark.parametrize("hamiltonian, params", HAMILTONIANS_AND_PARAMS)
    def test_ahs_program_from_evolution_no_local_detuning(self, hamiltonian, params):
        """Test that BraketLocalAhsDevice._ahs_program_from_evolution creates a valid
        AnalogHamiltonianSimulation when no local detuning is present."""

        evolution = ParametrizedEvolution(hamiltonian, params, 1.5)
        dev = BraketLocalAhsDevice(3)

        dev._validate_pulses(evolution.H.pulses)
        ahs_program = dev._ahs_program_from_evolution(evolution)

        assert isinstance(ahs_program, AnalogHamiltonianSimulation)

        # compare evolution and ahs_program registers
        assert ahs_program.register.coordinate_list(0) == [
            c[0] * 1e-6 for c in evolution.H.settings.register
        ]
        assert ahs_program.register.coordinate_list(1) == [
            c[1] * 1e-6 for c in evolution.H.settings.register
        ]

        # elements of the hamiltonian have the expected shape
        drive = ahs_program.hamiltonian
        assert isinstance(drive, DrivingField)

        amp_time = drive.amplitude.time_series.times()
        phase_time = drive.phase.time_series.times()
        det_time = drive.detuning.time_series.times()

        assert amp_time == phase_time == det_time
        assert amp_time[0] == evolution.t[0] * 1e-6
        assert amp_time[-1] == evolution.t[1] * 1e-6

    @pytest.mark.parametrize("hamiltonian, params", HAMILTONIANS_AND_PARAMS)
    @pytest.mark.parametrize(
        "local_detuning, local_params, local_wires",
        [(amp, [[0.5, 1.1, 2.9]], [0, 1]), (4.5, [], [1, 2])],
    )
    def test_ahs_program_from_evolution_with_local_detuning(
        self, hamiltonian, params, local_detuning, local_params, local_wires
    ):
        """Test that BraketLocalAhsDevice._ahs_program_from_evolution creates a valid
        AnalogHamiltonianSimulation when local detunings are present."""
        hamiltonian += rydberg_drive(0, 0, local_detuning, local_wires)
        params += local_params

        evolution = ParametrizedEvolution(hamiltonian, params, 1.5)
        dev = BraketLocalAhsDevice(3)

        dev._validate_pulses(evolution.H.pulses)
        ahs_program = dev._ahs_program_from_evolution(evolution)

        assert isinstance(ahs_program, AnalogHamiltonianSimulation)

        # compare evolution and ahs_program registers
        assert ahs_program.register.coordinate_list(0) == [
            c[0] * 1e-6 for c in evolution.H.settings.register
        ]
        assert ahs_program.register.coordinate_list(1) == [
            c[1] * 1e-6 for c in evolution.H.settings.register
        ]

        # elements of the hamiltonian have the expected shape
        h = ahs_program.hamiltonian

        assert isinstance(h, Hamiltonian)
        drive, shift = h.terms
        assert isinstance(drive, DrivingField)
        assert isinstance(shift, ShiftingField)

        amp_time = drive.amplitude.time_series.times()
        phase_time = drive.phase.time_series.times()
        det_time = drive.detuning.time_series.times()

        assert amp_time == phase_time == det_time
        assert amp_time[0] == evolution.t[0] * 1e-6
        assert amp_time[-1] == evolution.t[1] * 1e-6

        expected_pattern = [1 if i in local_wires else 0 for i in dev.wires]
        assert shift.magnitude.pattern.series == expected_pattern

        local_det_time = shift.magnitude.time_series.times()
        assert local_det_time[0] == evolution.t[0] * 1e-6
        assert local_det_time[-1] == evolution.t[1] * 1e-6


class TestBraketAwsAhsDevice:
    """Test functionality specific to the hardware device"""

    def test_initialize(self, mock_aws_device):
        """Test the device initializes with the expected attributes"""
        dev = mock_aws_device()

        assert dev._s3_folder == ("foo", "bar")
        assert dev.shots == 17
        assert dev.ahs_program is None
        assert dev.result is None
        assert dev.pennylane_requires == ">=0.30.0"
        assert dev.operations == {"ParametrizedEvolution"}
        assert dev.short_name == "braket.aws.ahs"

    def test_hardware_capabilities(self, mock_aws_device):
        """Test hardware capabilities can be retrieved"""

        dev = mock_aws_device()

        assert isinstance(dev.hardware_capabilities, dict)
        assert dev.hardware_capabilities == dict(PARADIGM_PROPERTIES)

    def test_settings(self, mock_aws_device):
        dev = mock_aws_device()
        assert list(dev.settings.keys()) == ["interaction_coeff"]
        assert np.isclose(dev.settings["interaction_coeff"], 862620)

    def test_validate_operations_multiple_drive_terms(self, mock_aws_device):
        """Test that an error is raised if there are multiple drive terms on
        the Hamiltonian"""
        dev = mock_aws_device()
        pulses = [HardwarePulse(3, 4, 5, [0, 1]), HardwarePulse(4, 6, 7, [1, 2])]

        with pytest.raises(
            NotImplementedError,
            match="Multiple pulses in a Hamiltonian are not currently supported",
        ):
            dev._validate_pulses(pulses)

    @pytest.mark.parametrize(
        "pulse_wires, dev_wires, res",
        [
            ([0, 1, 2], [0, 1, 2, 3], "error"),
            ([5, 6, 7, 8, 9], [4, 5, 6, 7, 8], "error"),
            ([0, 1, 2, 3, 6], [1, 2, 3], "error"),
            ([0, 1, 2], [0, 1, 2], "success"),
        ],
    )
    def test_validate_pulse_is_global_drive(self, mock_aws_device, pulse_wires, dev_wires, res):
        """Test that an error is raised if the pulse does not describe a global drive"""

        dev = mock_aws_device(wires=dev_wires)
        pulse = HardwarePulse(3, 4, 5, pulse_wires)

        if res == "error":
            with pytest.raises(
                NotImplementedError, match="Only global drive is currently supported"
            ):
                dev._validate_pulses([pulse])
        else:
            dev._validate_pulses([pulse])

    def test_get_rydberg_c6(self, mock_aws_device):
        """Test that _get_rydberg_c6 retrieves the c6 coefficient from the hardware properties
        and converts to expected PennyLane units"""

        dev = mock_aws_device()
        c6 = dev._get_rydberg_c6()

        assert np.isclose(
            float(2 * np.pi * c6 / 1e30),
            float(dev._device.properties.paradigm.rydberg.c6Coefficient),
        )

    @pytest.mark.parametrize("hamiltonian, params", HAMILTONIANS_AND_PARAMS)
    def test_create_ahs_program(self, hamiltonian, params, mock_aws_device):
        """Test that we can create an AnalogueHamiltonianSimulation from an
        evolution operator and store it on the device"""

        evolution = ParametrizedEvolution(hamiltonian, params, 1.5)
        dev = mock_aws_device()

        assert dev.ahs_program is None

        ahs_program = dev.create_ahs_program(evolution)

        # AHS program is created and stored on the device
        assert isinstance(dev.ahs_program, AnalogHamiltonianSimulation)

        # compare evolution and ahs_program registers
        assert np.allclose(
            [float(c) for c in ahs_program.register.coordinate_list(0)],
            [c[0] * 1e-6 for c in evolution.H.settings.register],
            atol=1e-6,
        )
        assert np.allclose(
            [float(c) for c in ahs_program.register.coordinate_list(1)],
            [float(c[1] * 1e-6) for c in evolution.H.settings.register],
            atol=1e-6,
        )

        # elements of the hamiltonian have the expected shape
        h = ahs_program.hamiltonian
        amp_time, amp_vals = h.amplitude.time_series.times(), h.amplitude.time_series.values()
        phase_time, phase_vals = h.phase.time_series.times(), h.phase.time_series.values()
        det_time, det_vals = h.detuning.time_series.times(), h.detuning.time_series.values()

        assert amp_time == phase_time == det_time
        assert float(amp_time[0]) == evolution.t[0] * 1e-6
        assert float(amp_time[-1]) == evolution.t[1] * 1e-6

        pulse = hamiltonian.pulses[0]
        params_idx = 0

        if callable(pulse.amplitude):
            fn = pulse.amplitude
            p = params[params_idx]
            params_idx += 1
            # atol 200 because of discretization, i.e. 27772.49134560574 --> 27600.0
            assert np.allclose(
                [fn(p, float(t) * 1e6) * 2 * np.pi * 1e6 for t in amp_time],
                [float(v) for v in amp_vals],
                atol=200,
            )
        else:
            assert np.allclose(
                [pulse.amplitude * 2 * np.pi * 1e6 for t in amp_time],
                [float(v) for v in amp_vals],
                atol=200,
            )

        if callable(pulse.phase):
            fn = pulse.phase
            p = params[params_idx]
            params_idx += 1
            assert np.allclose(
                [fn(p, float(t) * 1e6) for t in amp_time], [float(v) for v in phase_vals], atol=1e-7
            )
        else:
            assert np.allclose(
                [pulse.phase for t in amp_time], [float(v) for v in phase_vals], atol=1e-7
            )

        if callable(pulse.frequency):
            fn = pulse.frequency
            p = params[params_idx]
            params_idx += 1
            assert np.allclose(
                [fn(p, float(t) * 1e6) * 2 * np.pi * 1e6 for t in amp_time],
                [float(v) for v in det_vals],
            )
        else:
            assert np.allclose(
                [pulse.frequency * 2 * np.pi * 1e6 for t in amp_time], [float(v) for v in det_vals]
            )

    def test_run_task(self, mock_aws_device):
        """Tests that a (mock) task can be created"""
        dev = mock_aws_device()
        ahs_program = dummy_ahs_program()

        task = dev._run_task(ahs_program)

        assert isinstance(task, AwsQuantumTask)
