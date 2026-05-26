-- ============================================================
-- Project  : SchoolMS
-- Generated: 2026-05-25 11:55:41
-- Engine   : AI DB Schema Generator
-- Rules    : 109 production rules applied
-- ============================================================

SET SQL_MODE = "NO_AUTO_VALUE_ON_ZERO";
SET time_zone = "+00:00";
START TRANSACTION;

-- unique_id_header_all table
CREATE TABLE unique_id_header_all (
  id INT AUTO_INCREMENT PRIMARY KEY,
  table_name VARCHAR(100) NOT NULL,
  id_for VARCHAR(50) NOT NULL,
  prefix VARCHAR(20) NOT NULL,
  last_id VARCHAR(15) NOT NULL,
  created_on DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
  modified_on DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  INDEX idx_unique_id_header_all_table_name (table_name)
);

-- student_header_all table
CREATE TABLE student_header_all (
  id INT AUTO_INCREMENT PRIMARY KEY,
  student_id VARCHAR(20) NOT NULL,
  name VARCHAR(100) NOT NULL,
  email VARCHAR(100) NOT NULL,
  phone_number VARCHAR(20) NOT NULL,
  address VARCHAR(200) NOT NULL,
  batch_id INT NOT NULL,
  class_id INT NOT NULL,
  status TINYINT NOT NULL COMMENT '1=active, 2=inactive',
  created_on DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
  modified_on DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  INDEX idx_student_header_all_batch_id (batch_id),
  INDEX idx_student_header_all_class_id (class_id),
  CONSTRAINT fk_student_header_all_batch_header_all FOREIGN KEY (batch_id) REFERENCES batch_header_all (id),
  CONSTRAINT fk_student_header_all_class_header_all FOREIGN KEY (class_id) REFERENCES class_header_all (id)
);

-- batch_header_all table
CREATE TABLE batch_header_all (
  id INT AUTO_INCREMENT PRIMARY KEY,
  batch_id VARCHAR(20) NOT NULL,
  batch_name VARCHAR(100) NOT NULL,
  status TINYINT NOT NULL COMMENT '1=active, 2=inactive',
  created_on DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
  modified_on DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  INDEX idx_batch_header_all_batch_id (batch_id)
);

-- class_header_all table
CREATE TABLE class_header_all (
  id INT AUTO_INCREMENT PRIMARY KEY,
  class_id VARCHAR(20) NOT NULL,
  class_name VARCHAR(100) NOT NULL,
  status TINYINT NOT NULL COMMENT '1=active, 2=inactive',
  created_on DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
  modified_on DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  INDEX idx_class_header_all_class_id (class_id)
);

-- fee_transaction_all table
CREATE TABLE fee_transaction_all (
  id INT AUTO_INCREMENT PRIMARY KEY,
  student_id INT NOT NULL,
  fee_id INT NOT NULL,
  payment_mode_id INT NOT NULL,
  payment_date DATE NOT NULL,
  amount DECIMAL(10, 2) NOT NULL,
  cgst_amount DECIMAL(10, 2) NOT NULL,
  sgst_amount DECIMAL(10, 2) NOT NULL,
  closing_balance DECIMAL(12, 2) NOT NULL,
  payment_method VARCHAR(50) NOT NULL,
  status TINYINT NOT NULL COMMENT '1=paid, 2=unpaid',
  created_on DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
  modified_on DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  INDEX idx_fee_transaction_all_student_id (student_id),
  INDEX idx_fee_transaction_all_fee_id (fee_id),
  INDEX idx_fee_transaction_all_payment_mode_id (payment_mode_id),
  CONSTRAINT fk_fee_transaction_all_student_header_all FOREIGN KEY (student_id) REFERENCES student_header_all (id),
  CONSTRAINT fk_fee_transaction_all_fee_header_all FOREIGN KEY (fee_id) REFERENCES fee_header_all (id),
  CONSTRAINT fk_fee_transaction_all_payment_mode_configuration_all FOREIGN KEY (payment_mode_id) REFERENCES payment_mode_configuration_all (id)
);

-- payment_mode_configuration_all table
CREATE TABLE payment_mode_configuration_all (
  id INT AUTO_INCREMENT PRIMARY KEY,
  payment_mode_id VARCHAR(20) NOT NULL,
  payment_mode_name VARCHAR(100) NOT NULL,
  status TINYINT NOT NULL COMMENT '1=active, 2=inactive',
  created_on DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
  modified_on DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  INDEX idx_payment_mode_configuration_all_payment_mode_id (payment_mode_id)
);

-- fee_header_all table
CREATE TABLE fee_header_all (
  id INT AUTO_INCREMENT PRIMARY KEY,
  fee_id VARCHAR(20) NOT NULL,
  fee_name VARCHAR(100) NOT NULL,
  fee_amount DECIMAL(10, 2) NOT NULL,
  status TINYINT NOT NULL COMMENT '1=active, 2=inactive',
  created_on DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
  modified_on DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  INDEX idx_fee_header_all_fee_id (fee_id)
);

-- attendance_transaction_all table
CREATE TABLE attendance_transaction_all (
  id INT AUTO_INCREMENT PRIMARY KEY,
  student_id INT NOT NULL,
  attendance_date DATE NOT NULL,
  attendance_status TINYINT NOT NULL COMMENT '1=present, 2=absent',
  created_on DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
  modified_on DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  INDEX idx_attendance_transaction_all_student_id (student_id),
  CONSTRAINT fk_attendance_transaction_all_student_header_all FOREIGN KEY (student_id) REFERENCES student_header_all (id)
);

-- exam_header_all table
CREATE TABLE exam_header_all (
  id INT AUTO_INCREMENT PRIMARY KEY,
  exam_id VARCHAR(20) NOT NULL,
  exam_name VARCHAR(100) NOT NULL,
  exam_date DATE NOT NULL,
  status TINYINT NOT NULL COMMENT '1=active, 2=inactive',
  created_on DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
  modified_on DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  INDEX idx_exam_header_all_exam_id (exam_id)
);

-- quiz_header_all table
CREATE TABLE quiz_header_all (
  id INT AUTO_INCREMENT PRIMARY KEY,
  quiz_id VARCHAR(20) NOT NULL,
  quiz_name VARCHAR(100) NOT NULL,
  quiz_date DATE NOT NULL,
  status TINYINT NOT NULL COMMENT '1=active, 2=inactive',
  created_on DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
  modified_on DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  INDEX idx_quiz_header_all_quiz_id (quiz_id)
);

-- gst_invoice_header_all table
CREATE TABLE gst_invoice_header_all (
  id INT AUTO_INCREMENT PRIMARY KEY,
  invoice_id VARCHAR(20) NOT NULL,
  invoice_date DATE NOT NULL,
  student_id INT NOT NULL,
  fee_id INT NOT NULL,
  amount DECIMAL(10, 2) NOT NULL,
  cgst_amount DECIMAL(10, 2) NOT NULL,
  sgst_amount DECIMAL(10, 2) NOT NULL,
  status TINYINT NOT NULL COMMENT '1=active, 2=inactive',
  created_on DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
  modified_on DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  INDEX idx_gst_invoice_header_all_student_id (student_id),
  INDEX idx_gst_invoice_header_all_fee_id (fee_id),
  CONSTRAINT fk_gst_invoice_header_all_student_header_all FOREIGN KEY (student_id) REFERENCES student_header_all (id),
  CONSTRAINT fk_gst_invoice_header_all_fee_header_all FOREIGN KEY (fee_id) REFERENCES fee_header_all (id)
);

-- gst_configuration_all table
CREATE TABLE gst_configuration_all (
  id INT AUTO_INCREMENT PRIMARY KEY,
  gst_rate DECIMAL(5, 2) NOT NULL,
  cgst_rate DECIMAL(5, 2) NOT NULL,
  sgst_rate DECIMAL(5, 2) NOT NULL,
  status TINYINT NOT NULL COMMENT '1=active, 2=inactive',
  created_on DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
  modified_on DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP
);

-- teacher_header_all table
CREATE TABLE teacher_header_all (
  id INT AUTO_INCREMENT PRIMARY KEY,
  teacher_id VARCHAR(20) NOT NULL,
  name VARCHAR(100) NOT NULL,
  email VARCHAR(100) NOT NULL,
  phone_number VARCHAR(20) NOT NULL,
  address VARCHAR(200) NOT NULL,
  status TINYINT NOT NULL COMMENT '1=active, 2=inactive',
  created_on DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
  modified_on DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  INDEX idx_teacher_header_all_teacher_id (teacher_id)
);

-- salary_transaction_all table
CREATE TABLE salary_transaction_all (
  id INT AUTO_INCREMENT PRIMARY KEY,
  teacher_id INT NOT NULL,
  salary_date DATE NOT NULL,
  amount DECIMAL(10, 2) NOT NULL,
  cgst_amount DECIMAL(10, 2) NOT NULL,
  sgst_amount DECIMAL(10, 2) NOT NULL,
  status TINYINT NOT NULL COMMENT '1=paid, 2=unpaid',
  created_on DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
  modified_on DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  INDEX idx_salary_transaction_all_teacher_id (teacher_id),
  CONSTRAINT fk_salary_transaction_all_teacher_header_all FOREIGN KEY (teacher_id) REFERENCES teacher_header_all (id)
);

-- Archive tables
CREATE TABLE student_archive_all (
  id INT AUTO_INCREMENT PRIMARY KEY,
  student_id VARCHAR(20) NOT NULL,
  name VARCHAR(100) NOT NULL,
  email VARCHAR(100) NOT NULL,
  phone_number VARCHAR(20) NOT NULL,
  address VARCHAR(200) NOT NULL,
  batch_id INT NOT NULL,
  class_id INT NOT NULL,
  status TINYINT NOT NULL COMMENT '1=active, 2=inactive',
  created_on DATETIME NOT NULL,
  modified_on DATETIME NOT NULL,
  archived_on DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE fee_transaction_archive_all (
  id INT AUTO_INCREMENT PRIMARY KEY,
  student_id INT NOT NULL,
  fee_id INT NOT NULL,
  payment_mode_id INT NOT NULL,
  payment_date DATE NOT NULL,
  amount DECIMAL(10, 2) NOT NULL,
  cgst_amount DECIMAL(10, 2) NOT NULL,
  sgst_amount DECIMAL(10, 2) NOT NULL,
  closing_balance DECIMAL(12, 2) NOT NULL,
  payment_method VARCHAR(50) NOT NULL,
  status TINYINT NOT NULL COMMENT '1=paid, 2=unpaid',
  created_on DATETIME NOT NULL,
  modified_on DATETIME NOT NULL,
  archived_on DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE attendance_transaction_archive_all (
  id INT AUTO_INCREMENT PRIMARY KEY,
  student_id INT NOT NULL,
  attendance_date DATE NOT NULL,
  attendance_status TINYINT NOT NULL COMMENT '1=present, 2=absent',
  created_on DATETIME NOT NULL,
  modified_on DATETIME NOT NULL,
  archived_on DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE salary_transaction_archive_all (
  id INT AUTO_INCREMENT PRIMARY KEY,
  teacher_id INT NOT NULL,
  salary_date DATE NOT NULL,
  amount DECIMAL(10, 2) NOT NULL,
  cgst_amount DECIMAL(10, 2) NOT NULL,
  sgst_amount DECIMAL(10, 2) NOT NULL,
  status TINYINT NOT NULL COMMENT '1=paid, 2=unpaid',
  created_on DATETIME NOT NULL,
  modified_on DATETIME NOT NULL,
  archived_on DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
);

-- Life cycle tables
CREATE TABLE student_life_cycle_all (
  id INT AUTO_INCREMENT PRIMARY KEY,
  student_id INT NOT NULL,
  previous_status TINYINT NOT NULL COMMENT '1=active, 2=inactive',
  new_status TINYINT NOT NULL COMMENT '1=active, 2=inactive',
  status_change_date DATE NOT NULL,
  created_on DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
  modified_on DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  INDEX idx_student_life_cycle_all_student_id (student_id),
  CONSTRAINT fk_student_life_cycle_all_student_header_all FOREIGN KEY (student_id) REFERENCES student_header_all (id)
);

CREATE TABLE fee_transaction_life_cycle_all (
  id INT AUTO_INCREMENT PRIMARY KEY,
  fee_transaction_id INT NOT NULL,
  previous_status TINYINT NOT NULL COMMENT '1=paid, 2=unpaid',
  new_status TINYINT NOT NULL COMMENT '1=paid, 2=unpaid',
  status_change_date DATE NOT NULL,
  created_on DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
  modified_on DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  INDEX idx_fee_transaction_life_cycle_all_fee_transaction_id (fee_transaction_id),
  CONSTRAINT fk_fee_transaction_life_cycle_all_fee_transaction_all FOREIGN KEY (fee_transaction_id) REFERENCES fee_transaction_all (id)
);

CREATE TABLE attendance_transaction_life_cycle_all (
  id INT AUTO_INCREMENT PRIMARY KEY,
  attendance_transaction_id INT NOT NULL,
  previous_status TINYINT NOT NULL COMMENT '1=present, 2=absent',
  new_status TINYINT NOT NULL COMMENT '1=present, 2=absent',
  status_change_date DATE NOT NULL,
  created_on DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
  modified_on DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  INDEX idx_attendance_transaction_life_cycle_all_attendance_transaction_id (attendance_transaction_id),
  CONSTRAINT fk_attendance_transaction_life_cycle_all_attendance_transaction_all FOREIGN KEY (attendance_transaction_id) REFERENCES attendance_transaction_all (id)
);

CREATE TABLE salary_transaction_life_cycle_all (
  id INT AUTO_INCREMENT PRIMARY KEY,
  salary_transaction_id INT NOT NULL,
  previous_status TINYINT NOT NULL COMMENT '1=paid, 2=unpaid',
  new_status TINYINT NOT NULL COMMENT '1=paid, 2=unpaid',
  status_change_date DATE NOT NULL,
  created_on DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
  modified_on DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  INDEX idx_salary_transaction_life_cycle_all_salary_transaction_id (salary_transaction_id),
  CONSTRAINT fk_salary_transaction_life_cycle_all_salary_transaction_all FOREIGN KEY (salary_transaction_id) REFERENCES salary_transaction_all (id)
);

COMMIT;
-- ============================================================
-- End of schema
-- ============================================================
