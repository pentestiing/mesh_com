"""
This module contains CommsHSMController class which provides
HSM control functions for its client.
"""
import base64
import os
import random
import string
import subprocess
from datetime import datetime

import PyKCS11.LowLevel
from PyKCS11 import PyKCS11Error

from cryptography.hazmat.primitives import serialization
from cryptography.x509 import Certificate
from cryptography.x509 import load_der_x509_certificate

# To export pin
from Crypto.Protocol.KDF import PBKDF2
from Crypto.Hash import SHA256
from Crypto.Util.Padding import unpad
from Crypto.Cipher import AES


class CommsHSMController:
    """
    Comms HSM Controller class.
    """
    def __init__(self, base_dir: str, board_version: float) -> None:
        print("CommsHSMController: __init__")
        self.__pkcs11_session = None
        self.__token_user_pin = ""
        self.__token_so_pin = ""
        self.__base_dir = base_dir
        self.__user_pin_file = self.__base_dir + "/hsm/user_pin"
        self.__so_pin_file = self.__base_dir + "/hsm/so_pin"
        self.__comms_board_version = board_version

        self.__use_soft_hsm = False
        self.__login_required = False        # CKF_LOGIN_REQUIRED
        self.__so_pin_required = False       # CKF_SO_PIN_LOCKED
        self.__so_pin_to_be_changed = False  # CKF_SO_PIN_TO_BE_CHANGED
        self.__user_pin_initialized = False  # CKF_USER_PIN_INITIALIZED
        self.__token_has_rng = False         # CKF_RNG (Random Number Generator)

        # Used with SoftHSM and related PIN encryption
        self.__token_label = "secccoms"

        # Define configuration file for openssl
        os.environ['OPENSSL_CONF'] = self.__base_dir + "/comms_openssl.cnf"

        # Instantiate PyKCS11Lib
        self.__pkcs11 = PyKCS11.PyKCS11Lib()
        # Load library into use
        self.__pkcs11.load(self.__get_hsm_library())

    def open_session(self):
        """
        Opens PKCS11 control session.

        Returns:
            bool: True if session was opened, False otherwise.
        """
        print("CommsHSMController: open_session")
        self.__token_user_pin = self.__recover_pin(self.__user_pin_file)
        self.__token_so_pin = self.__recover_pin(self.__so_pin_file)
        self.__pkcs11_session = self.__get_pkcs11_session()

        # SoftHSM doesn't have valid token after flashing
        # but one needs to be created.
        if self.__pkcs11_session is None and self.__use_soft_hsm is True:
            self.__create_token(self.__token_label)
            self.__pkcs11_session = self.__get_pkcs11_session()

        if self.__pkcs11_session is None:
            return False
        # Success
        return True

    def close_session(self):
        """
        Closes PKCS11 control session
        """
        if self.__pkcs11_session:
            self.__pkcs11_session.closeSession()
            self.__pkcs11_session = None

    def __del__(self):
        self.close_session()

    def __get_hsm_library(self):

        # CM2.0 has SE050_C i.e. is using libsss_pkcs11.so
        if self.__comms_board_version == 1:
            path = "/usr/lib/libsss_pkcs11.so"
            if os.path.exists(path):
                self.__use_soft_hsm = False
                print(path)
                return path

        # Use SoftHSM for others
        paths_to_check = [
            "/usr/local/lib/softhsm/libsofthsm2.so",
            "/usr/lib/softhsm/libsofthsm2.so"
        ]

        for path in paths_to_check:
            if os.path.exists(path):
                self.__use_soft_hsm = True
                print(path)
                return path
        return None

    def __recover_pin(self, filename):
        try:
            with open(filename, 'r', encoding='utf-8') as file:
                pin_aux = file.readlines()  # need to make it absolute
            # Determine salt and ciphertext
            encryptedDataB64 = pin_aux[0].split('\n')[0]
            encryptedData = base64.b64decode(encryptedDataB64)
            salt = encryptedData[8:16]
            ciphertext = encryptedData[16:]
            # Reconstruct Key/IV-pair
            pbkdf2Hash = PBKDF2(self.__token_label, salt, 32 + 16, count=100000,
                                hmac_hash_module=SHA256)
            key = pbkdf2Hash[:32]
            iv = pbkdf2Hash[32:32 + 16]
            # Decrypt with AES-256 / CBC / PKCS7 Padding
            cipher = AES.new(key, AES.MODE_CBC, iv)
            return unpad(cipher.decrypt(ciphertext), 16).decode().split('\n')[0]
        except FileNotFoundError:
            print("No pin found")
            return ""

    def __generate_pin(self):
        pin_length = 6
        # Generate a random 6-digit PIN
        pin = ''.join(random.choices(string.digits, k=pin_length))
        return pin

    def __export_pin(self, pin, file_name, label):
        # Create directories if they don't exist
        os.makedirs(os.path.dirname(file_name), exist_ok=True)

        # Remove the output file if it already exists
        if os.path.isfile(file_name):
            os.remove(file_name)

        # Execute the openssl command to encrypt and export the PIN
        openssl_command = (
            f'echo "{pin}" | openssl enc -aes-256-cbc -md sha256 -a '
            f'-pbkdf2 -iter 100000 -salt -pass pass:{label}'
            f' > {file_name}'
        )

        ret = subprocess.run(openssl_command, shell=True)
        if ret.returncode != 0:
            print(str(ret.returncode) + str(ret.stdout) + str(ret.stderr))
            return False
        return True

    def __get_pkcs11_session(self):
        slots = self.__pkcs11.getSlotList(tokenPresent=True)
        for slot in slots:
            try:
                session = self.__pkcs11.openSession(
                    slot, PyKCS11.LowLevel.CKF_SERIAL_SESSION |
                    PyKCS11.LowLevel.CKF_RW_SESSION)

                # Check login requirements
                self.__get_token_info(slot)
                if self.__login_required:
                    if self.__user_pin_initialized is False:
                        print("login so user")
                        # Login as security officer
                        session.login(pin=self.__token_so_pin,
                                      user_type=PyKCS11.LowLevel.CKU_SO)
                        # Generate user pin for the token
                        self.__token_user_pin = self.__generate_pin()
                        # Encrypt and export pin for later usage
                        self.__export_pin(self.__token_user_pin,
                                          self.__user_pin_file,
                                          self.__token_label)
                        # Set user pin for the token
                        session.initPin(self.__token_user_pin)
                        # Log out Security Officer
                        session.logout()
                    # Log in as normal user
                    session.login(self.__token_user_pin, user_type=PyKCS11.LowLevel.CKU_USER)
                    # Refresh login requirements
                    self.__get_token_info(slot)
                print("so_pin", self.__token_so_pin)
                print("user_pin", self.__token_user_pin)
                return session
            except PyKCS11Error as e:
                print("exception:", e)
        return None

    def __create_token(self, token_name):
        slots = self.__pkcs11.getSlotList(tokenPresent=False)
        for slot in slots:
            try:
                self.__get_token_info(slot)
                if self.__so_pin_required:
                    # Generate security officer pin for the token
                    self.__token_so_pin = self.__generate_pin()
                    # Encrypt and export pins for later usage
                    self.__export_pin(self.__token_so_pin, self.__so_pin_file, token_name)
                # Initialize token
                self.__pkcs11.initToken(slot, self.__token_so_pin, token_name)
                break
            except PyKCS11Error as e:
                print("Create token exception:", e)

    def __get_token_info(self, slot):
        # Get token info
        token_info = self.__pkcs11.getTokenInfo(slot)

        # Get token name and strip out extra zeroes (seen at least with SoftHSM label names)
        self.__token_label = token_info.label.replace("\x00", "")

        # Set few member variables from token flags bitmask
        self.__login_required = bool(token_info.flags & PyKCS11.LowLevel.CKF_LOGIN_REQUIRED)
        self.__so_pin_required = bool(token_info.flags & PyKCS11.LowLevel.CKF_SO_PIN_LOCKED)
        self.__so_pin_to_be_changed = bool(
            token_info.flags & PyKCS11.LowLevel.CKF_SO_PIN_TO_BE_CHANGED)
        self.__user_pin_initialized = bool(
            token_info.flags & PyKCS11.LowLevel.CKF_USER_PIN_INITIALIZED)
        self.__token_has_rng = bool(token_info.flags & PyKCS11.LowLevel.CKF_RNG)

        print("login_required", self.__login_required)
        print("so_pin_required", self.__so_pin_required)
        print("so_pin_to_be_changed", self.__so_pin_to_be_changed)
        print("user_pin_initialized", self.__user_pin_initialized)
        print("token_has_rng", self.__token_has_rng)

    def save_certificate(self, cert_pem: Certificate, keypair_id, label: str):
        """
        Saves provided certificate into HSM using provided arguments.

        Arguments:
            cert_pem (Certificate) -- Certificate to save into HSM.
            keypair_id (str) -- ID number for the certificate object.
            label (str) -- Certificate label (used with SoftHSM only).

        Returns:
            bool: True if certificate was saved, False otherwise.
        """
        try:
            print("Certificate Subject:", cert_pem.subject)
            print("Certificate Issuer:", cert_pem.issuer.rfc4514_string())
            print("Certificate Not Before:", cert_pem.not_valid_before)
            print("Certificate Not After:", cert_pem.not_valid_after)
            print("Certificate Serial Number:", cert_pem.serial_number)
            print("Certificate version:", cert_pem.version)
            print("=" * 50)

            cka_id = bytes.fromhex(keypair_id)

            # Verify the validity of the received certificate
            current_time = datetime.now()
            if current_time > cert_pem.not_valid_after:
                print("Received certificate has already expired.")
                return False

            # Convert the PEM-encoded certificate to DER format
            certificate_data = cert_pem.public_bytes(serialization.Encoding.DER)

            # Convert the serial number integer to a byte string
            serial_number_int = cert_pem.serial_number
            serial_number_bytes = serial_number_int.to_bytes(
                (serial_number_int.bit_length() + 7) // 8, byteorder='big')

            # DER-encoded serial number: Tag + Length + Value
            serial_number_der = b"\x02" + bytes([len(serial_number_bytes)]) + serial_number_bytes

            # Fill the certificate template
            cert_template = [
                (PyKCS11.LowLevel.CKA_CLASS, PyKCS11.LowLevel.CKO_CERTIFICATE),
                (PyKCS11.LowLevel.CKA_PRIVATE, PyKCS11.CK_FALSE),
                (PyKCS11.LowLevel.CKA_LABEL, label),
                (PyKCS11.LowLevel.CKA_TOKEN, PyKCS11.CK_TRUE),
                (PyKCS11.LowLevel.CKA_CERTIFICATE_TYPE, PyKCS11.CKC_X_509),
                (PyKCS11.LowLevel.CKA_MODIFIABLE, PyKCS11.CK_TRUE),
                (PyKCS11.LowLevel.CKA_VALUE, certificate_data),
                (PyKCS11.LowLevel.CKA_SUBJECT, cert_pem.subject.rfc4514_string().encode('utf-8')),
                (PyKCS11.LowLevel.CKA_ID, cka_id),
                (PyKCS11.LowLevel.CKA_ISSUER, cert_pem.issuer.rfc4514_string().encode('utf-8')),
                (PyKCS11.LowLevel.CKA_START_DATE,
                 cert_pem.not_valid_before.strftime("%Y%m%d").encode('utf-8')),
                (PyKCS11.LowLevel.CKA_END_DATE,
                 cert_pem.not_valid_after.strftime("%Y%m%d").encode('utf-8')),
                (PyKCS11.LowLevel.CKA_SERIAL_NUMBER, serial_number_der),
            ]

            # Store certificate to HSM
            self.__pkcs11_session.createObject(cert_template)
            print("Certificate saved successfully.")
            return True

        except PyKCS11Error as e:
            print("Failed to save certificate:", e)
            return False

    def get_certificate(self, keypair_id, label):
        """
        Returns the first valid certificate in PEM format found with
        the given arguments.

        Arguments:
            keypair_id (str) -- Identifier number for the certicate.
            label (str) -- Label name for the certificate (used with SoftHSM only).

        Returns:
            str or None: Certificate in PEM format or None if not found.
        """
        cka_id = bytes.fromhex(keypair_id)
        # Get certificate objects by label and id
        certificate_objects = self.__pkcs11_session.findObjects(
            [(PyKCS11.LowLevel.CKA_CLASS, PyKCS11.LowLevel.CKO_CERTIFICATE),
             (PyKCS11.LowLevel.CKA_LABEL, label),
             (PyKCS11.LowLevel.CKA_ID, cka_id)])

        if len(certificate_objects) > 0:
            print(certificate_objects[0])
            for certificate_object in certificate_objects:
                certificate_bytes = bytes(
                    self.__pkcs11_session.getAttributeValue(certificate_object,
                                                            [PyKCS11.LowLevel.CKA_VALUE])[0])

                # Verify the validity of the certificate
                certificate = load_der_x509_certificate(certificate_bytes)
                current_time = datetime.now()
                if certificate.not_valid_before <= current_time <= certificate.not_valid_after:
                    # Return first valid certiticate as PEM format
                    return certificate.public_bytes(
                        encoding=serialization.Encoding.PEM).decode()
                else:
                    print("Certificate is not valid.")
                    # Delete invalid certificate
                    self.__pkcs11_session.destroyObject(certificate_object)
                    print("Invalid certificate has been deleted.")
        return None

    def has_private_key(self, keypair_id, label):
        """
        Finds a private key object from the HSM using the provided arguments.

        Arguments:
            keypair_id (str) -- Identifier number for the private key.
            label (str) -- Label name for the private key (used with SoftHSM only).

        Returns:
            bool: True if the private key exists, False otherwise.
        """
        cka_id = bytes.fromhex(keypair_id)

        filter_template = [
            (PyKCS11.LowLevel.CKA_CLASS, PyKCS11.LowLevel.CKO_PRIVATE_KEY),
            (PyKCS11.LowLevel.CKA_ID, cka_id)
        ]

        if self.__use_soft_hsm:
            filter_template.append((PyKCS11.LowLevel.CKA_LABEL, label))

        # Get private key objects by label and id
        priv_key_objects = self.__pkcs11_session.findObjects(filter_template)

        if len(priv_key_objects) > 0:
            # There is at least one matching object
            return True
        # Not found
        return False

    def has_public_key(self, keypair_id, label):
        """
        Finds a publick key object from the HSM using the provided arguments.

        Arguments:
            keypair_id (str) -- Identifier number for the publick key.
            label (str) -- Label name for the public key (used with SoftHSM only).

        Returns:
            bool: True if the public key exists, False otherwise.
        """
        cka_id = bytes.fromhex(keypair_id)

        filter_template = [
            (PyKCS11.LowLevel.CKA_CLASS, PyKCS11.LowLevel.CKO_PUBLIC_KEY),
            (PyKCS11.LowLevel.CKA_ID, cka_id)
        ]

        if self.__use_soft_hsm:
            filter_template.append((PyKCS11.LowLevel.CKA_LABEL, label))

        # Get public key objects by label and id
        pub_key_objects = self.__pkcs11_session.findObjects(filter_template)
        if len(pub_key_objects) > 0:
            # There is at least one matching object
            return True
        # Not found
        return False

    def generate_rsa_keypair(self, keypair_id: str, label: str) -> bool:
        """
        Generates an RSA keypair via HSM.

        Arguments:
            keypair_id (str) -- Identifier number for key objects.
            label (str) -- Label name for key objects (used with SoftHSM only).

        Returns:
            bool: True if the keypair was generated, False otherwise.
        """
        cka_id = bytes.fromhex(keypair_id)

        public_key_template = [
            # Uncomment "CKA_TOKEN" line in order
            # to store public key into HSM.
            (PyKCS11.LowLevel.CKA_CLASS, PyKCS11.LowLevel.CKO_PUBLIC_KEY),
            (PyKCS11.LowLevel.CKA_PRIVATE, PyKCS11.LowLevel.CK_FALSE),
            # (PyKCS11.LowLevel.CKA_TOKEN, PyKCS11.CK_TRUE),
            (PyKCS11.LowLevel.CKA_ENCRYPT, PyKCS11.LowLevel.CK_TRUE),
            (PyKCS11.LowLevel.CKA_VERIFY, PyKCS11.LowLevel.CK_TRUE),
            (PyKCS11.LowLevel.CKA_WRAP, PyKCS11.LowLevel.CK_TRUE),
            (PyKCS11.LowLevel.CKA_KEY_TYPE, PyKCS11.LowLevel.CKK_RSA),
            (PyKCS11.LowLevel.CKA_VERIFY_RECOVER, PyKCS11.LowLevel.CK_TRUE),
            (PyKCS11.LowLevel.CKA_MODULUS_BITS, 4096),
            (PyKCS11.LowLevel.CKA_ID, cka_id),
        ]

        private_key_template = [
            (PyKCS11.LowLevel.CKA_CLASS, PyKCS11.LowLevel.CKO_PRIVATE_KEY),
            (PyKCS11.LowLevel.CKA_PRIVATE, PyKCS11.LowLevel.CK_TRUE),
            (PyKCS11.LowLevel.CKA_TOKEN, PyKCS11.LowLevel.CK_TRUE),
            (PyKCS11.LowLevel.CKA_SENSITIVE, PyKCS11.LowLevel.CK_TRUE),
            (PyKCS11.LowLevel.CKA_DECRYPT, PyKCS11.LowLevel.CK_TRUE),
            (PyKCS11.LowLevel.CKA_SIGN, PyKCS11.LowLevel.CK_TRUE),
            (PyKCS11.LowLevel.CKA_UNWRAP, PyKCS11.LowLevel.CK_TRUE),
            (PyKCS11.LowLevel.CKA_ID, cka_id),
        ]

        # Label can be defined freely when using SoftHSM.
        if self.__use_soft_hsm:
            public_key_template.append((PyKCS11.LowLevel.CKA_LABEL, label))
            private_key_template.append((PyKCS11.LowLevel.CKA_LABEL, label))

        try:
            self.__pkcs11_session.generateKeyPair(public_key_template,
                                                  private_key_template,
                                                  PyKCS11.MechanismRSAGENERATEKEYPAIR)
        except PyKCS11Error as e:
            print("GenerateKeyPair exception:", e)
            return False
        else:
            print("Generated RSA keypair")
            return True

    def generate_ec_keypair(self, keypair_id: str, label: str) -> bool:
        """
        Generates an EC keypair via HSM.

        Arguments:
            keypair_id (str) -- Identifier number for key objects.
            label (str) -- Label name for key objects (used with SoftHSM only).

        Returns:
            bool: True if the keypair was generated, False otherwise.
        """
        cka_id = bytes.fromhex(keypair_id)

        # DER-encoded value for OID 1.2.840.10045.3.1.7 (prime256v1)
        ec_params = bytes.fromhex("06082A8648CE3D030107")

        public_key_template = [
            # Uncomment "CKA_TOKEN" line in order
            # to store public key into HSM.
            (PyKCS11.LowLevel.CKA_CLASS, PyKCS11.LowLevel.CKO_PUBLIC_KEY),
            (PyKCS11.LowLevel.CKA_PRIVATE, PyKCS11.LowLevel.CK_FALSE),
            (PyKCS11.LowLevel.CKA_TOKEN, PyKCS11.LowLevel.CK_TRUE),
            (PyKCS11.LowLevel.CKA_ENCRYPT, PyKCS11.LowLevel.CK_TRUE),
            (PyKCS11.LowLevel.CKA_VERIFY, PyKCS11.LowLevel.CK_TRUE),
            (PyKCS11.LowLevel.CKA_WRAP, PyKCS11.LowLevel.CK_TRUE),
            (PyKCS11.LowLevel.CKA_KEY_TYPE, PyKCS11.LowLevel.CKK_EC),
            (PyKCS11.LowLevel.CKA_EC_PARAMS, ec_params),
            (PyKCS11.LowLevel.CKA_ID, cka_id),
        ]

        private_key_template = [
            (PyKCS11.LowLevel.CKA_CLASS, PyKCS11.LowLevel.CKO_PRIVATE_KEY),
            (PyKCS11.LowLevel.CKA_KEY_TYPE, PyKCS11.LowLevel.CKK_EC),
            (PyKCS11.LowLevel.CKA_PRIVATE, PyKCS11.LowLevel.CK_TRUE),
            (PyKCS11.LowLevel.CKA_TOKEN, PyKCS11.LowLevel.CK_TRUE),
            (PyKCS11.LowLevel.CKA_SENSITIVE, PyKCS11.LowLevel.CK_TRUE),
            (PyKCS11.LowLevel.CKA_DECRYPT, PyKCS11.LowLevel.CK_TRUE),
            (PyKCS11.LowLevel.CKA_SIGN, PyKCS11.LowLevel.CK_TRUE),
            (PyKCS11.LowLevel.CKA_UNWRAP, PyKCS11.LowLevel.CK_TRUE),
            (PyKCS11.LowLevel.CKA_ID, cka_id),
        ]

        # Label can be defined freely when using SoftHSM.
        if self.__use_soft_hsm:
            public_key_template.append((PyKCS11.LowLevel.CKA_LABEL, label))
            private_key_template.append((PyKCS11.LowLevel.CKA_LABEL, label))

        try:
            self.__pkcs11_session.generateKeyPair(
                public_key_template,
                private_key_template,
                PyKCS11.MechanismECGENERATEKEYPAIR)

        except PyKCS11Error as e:
            print("GenerateKeyPair exception:", e)
            return False
        else:
            print("Generated EC keypair")
            return True

    def create_csr(self, priv_key_id: str, subject: str, filename: str):
        """
        Create a Certificate Signing Request (CSR) using OpenSSL with
        the provided arguments.

        Arguments:
            priv_key_id (str) -- Identifier number for the private key.
            subject (str) -- Subject to be used in CSR.
            filename (str) -- Output file name.

        Returns:
            bool: True if CSR generation is successful, False otherwise.
        """
        # Create directories if they don't exist
        os.makedirs(os.path.dirname(filename), exist_ok=True)

        # Openssl requires environment variable usage for pin
        os.environ['PKCS11_PIN'] = self.__token_user_pin

        # SoftHSM engine used by default
        pkcs_engine = 'pkcs11'
        if not self.__use_soft_hsm:
            pkcs_engine = 'e4sss'

        command = [
            'openssl',
            'req',
            '-new',
            '-engine', pkcs_engine,
            '-keyform', 'engine',
            '-key', str(priv_key_id),
            '-passin', 'env:PKCS11_PIN',
            '-out', str(filename),
            '-subj', str(subject)
            ]

        # Run the command
        result = subprocess.run(command, capture_output=True, text=True)

        # Check the result
        if result.returncode == 0:
            return True
        else:
            print("Command execution failed.")
            print("Error output:")
            print(result.stderr)
            return False
